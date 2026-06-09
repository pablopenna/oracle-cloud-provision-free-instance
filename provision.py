#!/usr/bin/env python3
"""Single-shot launcher for Oracle Cloud Always Free instances.

Tries each configured shape (A1.Flex first, then E2.1.Micro) across every
availability domain in the region, one attempt each, and stops on the first
successful launch. Designed to be run once and report results -- no retry loop.

Run with:  uv run provision.py
"""

from __future__ import annotations

import configparser
import os
import shutil
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import oci

PROJECT_DIR = Path(__file__).resolve().parent


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def fail(msg: str) -> "None":
    print(f"\n[ERROR] {msg}", file=sys.stderr)
    sys.exit(1)


def expand(path: str) -> Path:
    return Path(os.path.expanduser(path))


def load_user_config() -> dict:
    cfg_path = PROJECT_DIR / "config.toml"
    if not cfg_path.exists():
        # config.toml is optional: fall back to the committed example, whose
        # defaults work as-is from a valid OCI config (all OCIDs left empty).
        cfg_path = PROJECT_DIR / "config.example.toml"
        print(f"[cfg] no config.toml found -- using defaults from {cfg_path.name}")
    if not cfg_path.exists():
        fail("Neither config.toml nor config.example.toml found.")
    with cfg_path.open("rb") as fh:
        return tomllib.load(fh)


def resolve_oci_config_file(configured: str) -> Path:
    """Find the OCI auth config, supporting an in-repo ./.oci folder.

    Search order: the configured path, then ./.oci/config inside the repo, then
    ~/.oci/config.
    """
    candidates = [
        expand(configured),
        PROJECT_DIR / ".oci" / "config",
        expand("~/.oci/config"),
    ]
    for cand in candidates:
        if cand.is_file():
            return cand
    tried = "\n          ".join(str(c) for c in candidates)
    fail(
        "Could not find an OCI config file. Looked in:\n          " + tried + "\n"
        "        Create an API key in the OCI Console and save the config snippet,\n"
        "        e.g. at " + str(PROJECT_DIR / ".oci" / "config") + "."
    )


def build_oci_config(cfg_file: Path, profile: str, region_override: str) -> dict:
    # Parse the file ourselves rather than oci.config.from_file: the SDK loader
    # validates key_file relative to the *current working directory*, which would
    # reject a key_file that is relative to the config file's own folder (our
    # recommended setup). We resolve key_file against the config dir instead.
    parser = configparser.ConfigParser(inline_comment_prefixes=("#",))
    parser.read(cfg_file)
    if profile not in parser:
        fail(f"Profile [{profile}] not found in {cfg_file}")
    config = dict(parser[profile])

    # The config's key_file may be relative (handy when the config lives in-repo).
    # Resolve it against the config file's own directory and verify it exists.
    key_file = config.get("key_file")
    if key_file:
        key_path = expand(key_file)
        if not key_path.is_absolute():
            key_path = (cfg_file.parent / key_path).resolve()
        if not key_path.is_file():
            fail(f"Private key referenced by the OCI config not found: {key_path}")
        config["key_file"] = str(key_path)

    if region_override:
        config["region"] = region_override

    try:
        oci.config.validate_config(config)
    except oci.exceptions.InvalidConfig as exc:
        fail(f"Invalid OCI config: {exc}")
    return config


def ensure_ssh_public_key(pub_key_file: str) -> str:
    """Return the public key text, generating an ed25519 keypair if missing."""
    pub_path = expand(pub_key_file)
    if not pub_path.is_absolute():
        # A relative path (e.g. a public key committed in the repo) is resolved
        # against the project folder, not the current working directory.
        pub_path = PROJECT_DIR / pub_path
    if pub_path.exists():
        return pub_path.read_text().strip()

    if pub_path.suffix != ".pub":
        fail(f"ssh_public_key_file should end in .pub, got: {pub_path}")

    priv_path = pub_path.with_suffix("")  # drop the .pub
    print(f"[ssh] No key at {pub_path} -- generating an ed25519 keypair...")
    priv_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                "ssh-keygen", "-t", "ed25519",
                "-f", str(priv_path),
                "-N", "",
                "-C", "oracle-cloud-free-vm",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        fail("ssh-keygen not found on PATH; cannot generate the key.")
    except subprocess.CalledProcessError as exc:
        fail(f"ssh-keygen failed: {exc.stderr.strip()}")
    print(f"[ssh] Created {priv_path} and {pub_path}")

    # Mirror the public key into the project folder so it can be committed and
    # used by the GitHub Actions workflow (a public key is not secret). The
    # private key stays put -- only the .pub is copied.
    repo_pub = PROJECT_DIR / pub_path.name
    if pub_path != repo_pub:
        shutil.copy(pub_path, repo_pub)
        print(f"[ssh] Copied public key into the repo: {repo_pub.name} (commit it for CI)")

    return pub_path.read_text().strip()


def find_newest_image(compute, compartment_id, shape, os_name, os_version):
    """Newest image for the given OS compatible with the shape (arch handled by OCI)."""
    kwargs = dict(
        compartment_id=compartment_id,
        operating_system=os_name,
        shape=shape,
        sort_by="TIMECREATED",
        sort_order="DESC",
        lifecycle_state="AVAILABLE",
    )
    if os_version:
        kwargs["operating_system_version"] = os_version
    images = compute.list_images(**kwargs).data
    return images[0] if images else None


def is_capacity_error(exc: oci.exceptions.ServiceError) -> bool:
    msg = (exc.message or "").lower()
    return "out of host capacity" in msg or "out of capacity" in msg


def find_existing_instances(compute, compartment_id, shape_names):
    """Map each target shape to a live instance of that shape, if one exists.

    Used to avoid launching a second instance of a shape we already have, while
    still allowing other shapes to be attempted. Terminated/terminating
    instances are ignored (they free up the quota).
    """
    dead = {"TERMINATED", "TERMINATING"}
    found: dict[str, object] = {}
    for inst in compute.list_instances(compartment_id=compartment_id).data:
        if inst.lifecycle_state in dead or inst.shape not in shape_names:
            continue
        found.setdefault(inst.shape, inst)
    return found


# --------------------------------------------------------------------------- #
# Networking: reuse a public subnet, or build the whole stack
# --------------------------------------------------------------------------- #
def find_public_subnet(network, compartment_id):
    """Return an existing subnet that truly reaches the internet, or None.

    "Public internet access" means: the subnet assigns public IPs AND its route
    table sends 0.0.0.0/0 to an *enabled* internet gateway.
    """
    for vcn in network.list_vcns(compartment_id=compartment_id).data:
        igw_ids = {
            g.id
            for g in network.list_internet_gateways(
                compartment_id=compartment_id, vcn_id=vcn.id
            ).data
            if g.is_enabled and g.lifecycle_state == "AVAILABLE"
        }
        if not igw_ids:
            continue
        for sn in network.list_subnets(compartment_id=compartment_id, vcn_id=vcn.id).data:
            if sn.lifecycle_state != "AVAILABLE" or sn.prohibit_public_ip_on_vnic:
                continue
            rt = network.get_route_table(sn.route_table_id).data
            for rule in rt.route_rules:
                if rule.destination == "0.0.0.0/0" and rule.network_entity_id in igw_ids:
                    return sn
    return None


def create_public_subnet(network, compartment_id):
    """Create VCN + internet gateway + default route + a regional public subnet."""
    composite = oci.core.VirtualNetworkClientCompositeOperations(network)

    print("[net] creating VCN free-vm-vcn (10.0.0.0/16) ...")
    vcn = composite.create_vcn_and_wait_for_state(
        oci.core.models.CreateVcnDetails(
            compartment_id=compartment_id,
            cidr_block="10.0.0.0/16",
            display_name="free-vm-vcn",
        ),
        wait_for_states=["AVAILABLE"],
    ).data

    print("[net] creating internet gateway ...")
    igw = composite.create_internet_gateway_and_wait_for_state(
        oci.core.models.CreateInternetGatewayDetails(
            compartment_id=compartment_id,
            vcn_id=vcn.id,
            is_enabled=True,
            display_name="free-vm-igw",
        ),
        wait_for_states=["AVAILABLE"],
    ).data

    print("[net] routing 0.0.0.0/0 -> internet gateway ...")
    network.update_route_table(
        vcn.default_route_table_id,
        oci.core.models.UpdateRouteTableDetails(
            route_rules=[
                oci.core.models.RouteRule(
                    destination="0.0.0.0/0",
                    destination_type="CIDR_BLOCK",
                    network_entity_id=igw.id,
                )
            ]
        ),
    )

    print("[net] creating public subnet (10.0.1.0/24) ...")
    # No availability_domain => regional subnet (usable from any AD).
    # No security_list_ids => inherits the VCN default list, which permits SSH (22).
    subnet = composite.create_subnet_and_wait_for_state(
        oci.core.models.CreateSubnetDetails(
            compartment_id=compartment_id,
            vcn_id=vcn.id,
            cidr_block="10.0.1.0/24",
            display_name="free-vm-subnet",
            route_table_id=vcn.default_route_table_id,
            prohibit_public_ip_on_vnic=False,
        ),
        wait_for_states=["AVAILABLE"],
    ).data
    print(f"[net] subnet ready: {subnet.id}")
    return subnet


def ensure_subnet(network, compartment_id, configured_subnet_id):
    """Resolve the subnet to launch into: explicit config, else reuse, else create."""
    if configured_subnet_id and "xxxxx" not in configured_subnet_id:
        sn = network.get_subnet(configured_subnet_id).data
        print(f"[net] using subnet from config: {sn.id}")
        return sn

    existing = find_public_subnet(network, compartment_id)
    if existing is not None:
        print(f"[net] reusing existing public subnet: {existing.id}")
        return existing

    print("[net] no internet-connected subnet found -- creating one ...")
    return create_public_subnet(network, compartment_id)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    user_cfg = load_user_config()

    oci_cfg_file = resolve_oci_config_file(user_cfg.get("oci_config_file", "~/.oci/config"))
    config = build_oci_config(
        oci_cfg_file,
        user_cfg.get("oci_profile", "DEFAULT"),
        user_cfg.get("region", "").strip(),
    )

    # compartment_id is optional: default to the tenancy OCID from the API key
    # config (the tenancy is the root compartment). region likewise comes from
    # the config unless overridden above.
    compartment_id = user_cfg.get("compartment_id", "").strip()
    if not compartment_id or "xxxxx" in compartment_id:
        compartment_id = config.get("tenancy", "").strip()
        if not compartment_id:
            fail("No compartment_id set and no tenancy found in the OCI config.")
        print(f"[oci] compartment_id not set -- using tenancy (root compartment)")
    print(f"[oci] config: {oci_cfg_file}  region: {config['region']}")
    print(f"[oci] compartment: {compartment_id}")

    pub_key = ensure_ssh_public_key(user_cfg["ssh_public_key_file"])

    identity = oci.identity.IdentityClient(config)
    compute = oci.core.ComputeClient(config)
    network = oci.core.VirtualNetworkClient(config)

    shapes = user_cfg.get("shapes", [])
    if not shapes:
        fail("No [[shapes]] defined in config.")
    shape_names = {s["name"] for s in shapes}

    # Don't launch a second instance of a shape we already have (enabled by
    # default). This is per-shape: an existing A1 won't block an E2 attempt and
    # vice versa. Runs before any networking so a fully-provisioned account
    # makes scheduled runs cheap no-ops.
    skip_shapes: set[str] = set()
    if bool(user_cfg.get("skip_if_instance_exists", True)):
        existing_by_shape = find_existing_instances(compute, compartment_id, shape_names)
        for shape, inst in existing_by_shape.items():
            skip_shapes.add(shape)
            print(
                f"\n[skip] {shape} already exists -- not launching another.\n"
                f"       name:  {inst.display_name}\n"
                f"       state: {inst.lifecycle_state}\n"
                f"       id:    {inst.id}\n"
                f"       (Set skip_if_instance_exists = false in config.toml to override.)"
            )
        if skip_shapes >= shape_names:
            print("\n[skip] All target shapes already exist -- nothing to do.")
            return

    # Resolve (or auto-create) a subnet with public internet access.
    subnet = ensure_subnet(network, compartment_id, user_cfg.get("subnet_id", "").strip())
    subnet_id = subnet.id

    ads = identity.list_availability_domains(compartment_id=compartment_id).data
    if not ads:
        fail("No availability domains returned -- check compartment_id and region.")
    ad_names = [ad.name for ad in ads]
    # An AD-specific subnet can only host instances in its own AD.
    if getattr(subnet, "availability_domain", None):
        ad_names = [subnet.availability_domain]
    print(f"[oci] availability domains to try: {', '.join(ad_names)}")

    os_name = user_cfg.get("image_operating_system", "Canonical Ubuntu")
    os_version = user_cfg.get("image_operating_system_version", "")
    prefix = user_cfg.get("display_name_prefix", "free")
    assign_public_ip = bool(user_cfg.get("assign_public_ip", True))

    attempts: list[tuple[str, str, str]] = []  # (shape, ad, outcome)

    for shape_cfg in shapes:
        shape = shape_cfg["name"]
        if shape in skip_shapes:
            attempts.append((shape, "-", "already exists -- skipped"))
            continue
        image = find_newest_image(compute, compartment_id, shape, os_name, os_version)
        if image is None:
            os_label = f"{os_name} {os_version}".strip()
            print(f"\n=== {shape}: no '{os_label}' image found, skipping ===")
            attempts.append((shape, "-", "no compatible image"))
            continue

        print(f"\n=== {shape} ===")
        print(f"    image: {image.display_name} ({image.id})")

        shape_config = None
        if "ocpus" in shape_cfg or "memory_in_gbs" in shape_cfg:
            shape_config = oci.core.models.LaunchInstanceShapeConfigDetails(
                ocpus=float(shape_cfg["ocpus"]),
                memory_in_gbs=float(shape_cfg["memory_in_gbs"]),
            )

        for ad in ad_names:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            short = shape.split(".")[-1].lower()
            details = oci.core.models.LaunchInstanceDetails(
                compartment_id=compartment_id,
                availability_domain=ad,
                shape=shape,
                display_name=f"{prefix}-{short}-{stamp}",
                source_details=oci.core.models.InstanceSourceViaImageDetails(image_id=image.id),
                create_vnic_details=oci.core.models.CreateVnicDetails(
                    subnet_id=subnet_id,
                    assign_public_ip=assign_public_ip,
                ),
                metadata={"ssh_authorized_keys": pub_key},
            )
            if shape_config is not None:
                details.shape_config = shape_config

            print(f"    -> launching in {ad} ...", end=" ", flush=True)
            try:
                inst = compute.launch_instance(details).data
            except oci.exceptions.ServiceError as exc:
                if is_capacity_error(exc):
                    print("OUT OF CAPACITY")
                    attempts.append((shape, ad, "out of capacity"))
                    continue
                if exc.code == "LimitExceeded" or exc.status == 400 and "limit" in (exc.message or "").lower():
                    print(f"LIMIT: {exc.message}")
                    attempts.append((shape, ad, f"limit: {exc.message}"))
                    continue
                print(f"ERROR {exc.status} {exc.code}: {exc.message}")
                attempts.append((shape, ad, f"error: {exc.code}"))
                continue

            print("SUCCESS")
            print("\n" + "=" * 60)
            print("  INSTANCE LAUNCHING")
            print("=" * 60)
            print(f"  name:  {inst.display_name}")
            print(f"  shape: {inst.shape}")
            print(f"  id:    {inst.id}")
            print(f"  state: {inst.lifecycle_state}")
            print("\n  Check the OCI Console for the public IP once it is RUNNING,")
            print("  then: ssh -i ~/.ssh/oracle_cloud_free_vm ubuntu@<public-ip>")
            print_summary(attempts + [(shape, ad, "SUCCESS")])
            return

    print_summary(attempts)
    print("\nNo instance created this run. Capacity for the free shapes (A1 especially)")
    print("frees up sporadically -- re-run later, or schedule this script periodically.")
    sys.exit(2)


def print_summary(attempts: list[tuple[str, str, str]]) -> None:
    print("\n--- attempt summary ---")
    for shape, ad, outcome in attempts:
        print(f"  {shape:<26} {ad:<22} {outcome}")


if __name__ == "__main__":
    main()
