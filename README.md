# ChangeSlip

Free, self-hosted change-order slips for local service owners. Type what the customer wants, price it, send a magic link, get Accept before work continues, keep a timestamped audit trail.

No signup. No license. One Docker Compose service and a SQLite file. About 15 minutes on a 1GB VPS.

## What it does

- Create a change slip (customer, summary, amount in cents, optional email/phone/job ref/details)
- Share an unguessable public link `{PUBLIC_BASE_URL}/c/{token}` — Copy link, Copy SMS/text blurb, Copy email, or optional SMTP Email link
- Customer Accept or Decline on a mobile-friendly page (no login; the magic link is the credential)
- Owner detail page with append-only audit timeline (`created`, `link_emailed`, `accepted`, `declined`, `voided`)
- Printable receipt for accepted slips at `/c/{token}/receipt`
- Void pending slips; accepted slips cannot be voided
- `GET /health` → HTTP 200 `{"status":"ok","smtp_configured":false}` even when SMTP is unset

Without SMTP you still get in-app status and can copy the link / blurbs. Owner notify email and Email link stay unused until `SMTP_HOST` is set.

## Privacy

Self-hosted. You run the box; the owner is the data controller for customer names and contact fields. No Stripe, no SMS gateway, no third-party analytics SaaS. Accept records optional IP / user-agent on your own SQLite file.

## 15-minute Ubuntu VPS install

Documented on **Ubuntu 22.04 / 24.04**. About 15 minutes.

**Debian 13:** do **not** run the Ubuntu `docker-ce` recipe below on Debian. Use the distro packages instead:

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose
sudo usermod -aG docker "$USER"
```

Log out and back in (or `newgrp docker`). On Debian, start the stack with `docker-compose` (hyphen) if `docker compose` is not available.

**Amazon Linux:** not documented yet. Use Ubuntu or Debian.

### 1. Install Docker Engine and the Compose plugin (Ubuntu only)

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo ${UBUNTU_CODENAME:-$VERSION_CODENAME}) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo usermod -aG docker "$USER"
```

Log out and back in (or run `newgrp docker`) so `docker` works without `sudo`.

### 2. Clone, configure, start

```bash
git clone https://github.com/aidendify/changeslip.git
cd changeslip
cp .env.example .env
```

Edit `.env` and set at least `BUSINESS_NAME`, `PUBLIC_BASE_URL`, `SECRET_KEY`, and `OWNER_PASSWORD`. Leave `SMTP_*` and `MARKETING_URL` empty unless configured. Set `OWNER_PASSWORD` on any VPS reachable from the internet (empty means the admin UI is open).

```bash
docker compose up --build -d
```

(On Debian, `docker-compose up --build -d` if the Compose plugin is not installed.)

The app binds `0.0.0.0:8080` in the container. Compose maps host `8080:8080`. SQLite lives on the `changeslip-data` volume at `/data/changeslip.db`.

### 3. Smoke test

Use this `.env` for a first pass (Verifier values). Production should use a real `SECRET_KEY` and `OWNER_PASSWORD`. Do not bake these test passwords as production defaults.

```
OWNER_PASSWORD=testpass
PUBLIC_BASE_URL=http://localhost:8080
BUSINESS_NAME=Harbor HVAC
CURRENCY=USD
REQUIRE_TYPED_NAME=false
MARKETING_URL=
SECRET_KEY=change-me
OWNER_NOTIFY_EMAIL=
```

Leave all `SMTP_*` unset.

1. Healthcheck:

   ```bash
   curl -sf http://localhost:8080/health
   ```

   Expected: JSON containing `"status":"ok"`, `"smtp_configured":false`, HTTP 200.

2. Open http://localhost:8080, log in with `testpass`, create a slip with summary **Add whole-house surge protector** and amount **25000** cents ($250.00). Copy the `/c/{token}` link from the detail page.

3. Open the public link (second browser or phone). Confirm summary, `$250.00`, Accept and Decline. Click **Accept**.

4. Owner detail shows status `accepted`, timeline has `created` + `accepted`. Open `/c/{token}/receipt` — amount and accepted time appear.

5. A second Accept does not add a duplicate `accepted` event. Void only works on pending slips.

See `sample-slip.md` for example field values.

## Configuration

Copy `.env.example` to `.env` before `docker compose up`. Variables:

| Variable | Purpose |
| --- | --- |
| `PORT` | Documented as 8080. The container always binds gunicorn to `0.0.0.0:8080`. |
| `DATABASE_PATH` | SQLite file. Compose overrides this to `/data/changeslip.db`. |
| `SECRET_KEY` | Flask session key. Change it on a public VPS. |
| `OWNER_PASSWORD` | Admin login. Empty = open admin (local/dev). Set this on any internet-reachable VPS. |
| `BUSINESS_NAME` | Public page + receipt. |
| `PUBLIC_BASE_URL` | No trailing slash. Used in magic links, e.g. `http://localhost:8080`. |
| `CURRENCY` | Default display currency for new slips (`USD`). |
| `REQUIRE_TYPED_NAME` | If `true`, Accept requires typed name matching `customer_name` (case-insensitive trim). Default `false`. |
| `FROM_NAME`, `FROM_EMAIL` | SMTP From / email blurb sign-off. |
| `OWNER_NOTIFY_EMAIL` | Accept/decline alert destination when SMTP is set. Empty → in-app only. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_TLS` | Optional send. If `SMTP_HOST` is unset, Email link and owner email notify are unused. |
| `MARKETING_URL` | If set, footer link **Powered by ChangeSlip** points here. If unset, there is no footer. |

Do not commit `.env`. SMTP passwords and `OWNER_PASSWORD` are never written to application logs.

## Healthcheck

`GET /health` → HTTP 200:

```json
{"status":"ok","smtp_configured":false}
```

`smtp_configured` is `true` only when `SMTP_HOST` is set. Health succeeds even when SMTP is unset. This route never requires login.

## Local development (optional)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_PATH=./changeslip.db
python app.py
```

Then open http://localhost:8080. This path is for hacking on the code; the supported install is Docker Compose.

## What this is not

ChangeSlip is **not** a Nudge-style multi-day drip (no Day 0/3/7 sequence). It is **not** AfterJob (no CSAT / Google review ask). It is **not** FormFirst (no contact-form webhook). It is **not** OpenPing (no quote open-tracking).

No Stripe / payment collection, no SMS / Twilio, no LLM, no Redis, no Celery, no second Compose service.
