# oracle-cloud-provision-free-instance

A single-shot Python script that tries to launch an Oracle Cloud **Always Free**
instance. It attempts `VM.Standard.A1.Flex` first, then `VM.Standard.E2.1.Micro`,
across every availability domain in your region, and stops on the first success.

It runs **once** and reports results (no retry loop) — the typical "Out of host
capacity" error doesn't change second-to-second. Re-run later (or schedule it) to
keep trying; A1 capacity frees up sporadically.

Everything lives in this folder so it's safe to keep in Git. Secrets and your
real config are git-ignored.

## Prerequisites

Before running the script for the first time, make sure you have all of the
following. Items 2 and 4 are walked through in detail in the numbered sections
below.

1. **An Oracle Cloud account** with the Always Free tier active (a verified
   account; even "Pay As You Go" upgrades keep the Always Free resources).
2. **`uv` installed** on your machine (this repo uses it to install deps locally).
   Install: `curl -LsSf https://astral.sh/uv/install.sh | sh` — see
   <https://docs.astral.sh/uv/>.
3. **`ssh-keygen` available** on your PATH (ships with OpenSSH; needed only if you
   let the script generate the login key for you).
4. **An OCI API signing key** created in the Console, with its config snippet and
   private key saved where the script can find them (§2).
That's it for required setup. The script handles the rest from the API key:

- **Networking** — it reuses an existing subnet that has public internet access,
  or, if none exists, **creates the whole stack for you** (VCN + internet gateway
  + default route + a regional public subnet that permits SSH).
- **Region** and **compartment** — read automatically from the OCI config (the
  tenancy in that config is your root compartment). Override either only if you
  want a specific region or sub-compartment.
- **SSH key** — generated automatically if it doesn't exist.

Checklist of values you'll paste into `config.toml`: just the path to your OCI
API config file (and only if it's not in one of the default locations).
Everything else is optional.

## 1. Install dependencies (locally, via uv)

```bash
uv sync          # creates ./.venv with the oci SDK inside this folder
```

## 2. Get OCI API credentials

In the **OCI Console**: profile icon (top-right) → **My profile** → **API keys**
→ **Add API key** → *Generate API key pair* → **Download private key** → **Add**.

> **Note:** only the **private** key is needed. The matching public key shown on
> the same screen does *not* need to be downloaded — Oracle keeps it server-side
> and the SDK authenticates using the private key plus the fingerprint. (This is
> the OCI *API signing* key, unrelated to the SSH key used to log into the VM.)

Oracle then shows a **Configuration file preview**. Save that snippet to one of
the locations the script auto-detects (no need to set anything in `config.toml`):

| Location | Notes |
| --- | --- |
| `~/.oci/config` | standard OCI location (default) |
| `./.oci/config` | inside the repo (git-ignored) |

**Auto-detection:** the script looks for the config in this order and uses the
first that exists — the `oci_config_file` value in `config.toml`, then
`./.oci/config`, then `~/.oci/config`. So you only set `oci_config_file` if you
keep the config somewhere non-standard; otherwise leave it untouched.

### Point the config at your private key (`key_file`)

The config snippet from Oracle ends with a `key_file=` line that ships as a
placeholder you **must** edit:

```ini
key_file=<path to your private keyfile> # TODO   ← replace this whole value
```

Set it to wherever you saved the downloaded `.pem`. Three valid forms:

| Form | Example | Resolved against |
| --- | --- | --- |
| **Filename only** (recommended) | `key_file=oci_api_key.pem` | the config file's own folder |
| **Relative path** | `key_file=keys/oci_api_key.pem` | the config file's own folder |
| **Absolute path** | `key_file=/home/pablo/.oci/oci_api_key.pem` | n/a |
| **Home-relative** | `key_file=~/.oci/oci_api_key.pem` | your home directory |

The simplest setup is to keep the key **next to the config** (e.g. both in
`./.oci/`) and use just the filename. Whatever you choose, drop the placeholder's
trailing ` # TODO` comment.

Finally, lock the key down: `chmod 600 <key>.pem`.

## 3. Configure the launch (Optional)

```bash
cp config.example.toml config.toml
```

All fields save for in `config.toml` are **optional** — the defaults work from just a
valid OCI API config. You may set:

- `subnet_id` — pin a specific subnet. Empty ⇒ reuse a public subnet or create
  one automatically.
- `compartment_id` — empty ⇒ use your tenancy (root compartment) from the OCI
  config. Set only for a sub-compartment.
- `region` — override (e.g. `eu-frankfurt-1`); empty ⇒ region from the OCI config.

The script auto-discovers availability domains and the newest **Ubuntu 24.04**
image compatible with each shape, auto-handles networking, and **generates the
SSH key** (`~/.ssh/oracle_cloud_free_vm.pub` by default) if it doesn't exist.

## 4. Run

```bash
uv run provision.py
```

On success it prints the instance OCID and state. Grab the public IP from the OCI
Console once it's `RUNNING`, then:

```bash
ssh -i ~/.ssh/oracle_cloud_free_vm ubuntu@<public-ip>
```

Exit codes: `0` launched, `2` no capacity / nothing launched, `1` configuration error.

## Scheduling (optional)

To keep trying automatically, add a cron entry, e.g. every 15 minutes:

```cron
*/15 * * * * cd /home/pablo/code/oracle-cloud-provision-free-instance && uv run provision.py >> run.log 2>&1
```

(Remove it once you've got your instance — you only get a limited free quota.)
