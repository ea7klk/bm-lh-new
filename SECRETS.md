# Secrets and key rotation

The repository contains **no secret material** — not plaintext, not encrypted.
Every workload reads a Kubernetes Secret that was created directly in the
cluster, referenced by name from `fleet/bminfo/workloads.yaml` via
`envFrom.secretRef` or `imagePullSecrets`. The manifest therefore describes
*which* secret a pod reads; the values never enter Git.

This replaced the SOPS-era arrangement, in which `gitops/production` carried
age-encrypted copies of the same manifests and a `.sops.yaml` recipient rule.
That tree was a handoff record from the `spainip/infra` migration and was never
applied — Rancher Fleet has been the only controller since adoption — so it,
`.sops.yaml`, and the `ksops` generator were deleted. Nothing in the deploy path
now depends on an age key, a `sops` binary, or the ability to decrypt anything.
Rotating a secret means writing the new value into the cluster; there is no
ciphertext to re-encrypt and no key ceremony to perform.

## Inventory

| Secret | Read by | Keys that matter |
| --- | --- | --- |
| `web-env` | `web`, `collector`, all four CronJobs | `APP_SECRET_KEY`, `ADMIN_PASSWORD`, `DB_PASSWORD`, `SMTP_USERNAME`, `SMTP_PASSWORD` |
| `postgres-env` | `postgres` | `POSTGRES_PASSWORD` (used only when initialising an empty data directory) |
| `pgadmin-env` | `pgadmin` | `PGADMIN_DEFAULT_EMAIL`, `PGADMIN_DEFAULT_PASSWORD` |
| `bminfo-object-store` | `web`, `archive-raw-events` | `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_ENDPOINT_URL`, `S3_BUCKET` |
| `registry-credentials` | every pod (`imagePullSecrets`) | `.dockerconfigjson` |

`web-env` also carries non-secret configuration (`DB_HOST`, `APP_WORKERS`,
`SMTP_FROM_NAME`, and so on). It is a Secret because `APP_SECRET_KEY` shares the
object; that does not make every key in it sensitive.

Values are never printed here. Read one with:

```bash
kubectl get secret web-env -n bminfo -o jsonpath='{.data.APP_SECRET_KEY}' | base64 -d; echo
```

## The signing key

`APP_SECRET_KEY` signs session cookies and password-reset tokens. Outside
`DEBUG`, `config/settings.py` refuses to start unless it is at least 50
characters; there is no fallback to the admin password, which is how production
once ended up signing cookies with a nine-character string for two months
without anything failing (REVIEW.md H1).

An empty or short key now raises `ImproperlyConfigured` at import, so the pod
crashloops and fails its readiness probe. That is the intended outcome: a
deployment that cannot sign securely should not serve traffic.

## Creating the secrets from scratch

Names must match the `secretRef` values in `workloads.yaml` exactly.

```bash
kubectl create namespace bminfo --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret generic web-env -n bminfo \
  --from-literal=APP_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(64))')" \
  --from-literal=ADMIN_PASSWORD="$(python -c 'import secrets; print(secrets.token_urlsafe(24))')" \
  --from-literal=DB_HOST=postgres --from-literal=DB_PORT=5432 \
  --from-literal=DB_NAME=bminfo --from-literal=DB_USER=bminfo \
  --from-literal=DB_PASSWORD='<value chosen below>' \
  # ... remainder of the keys listed in .env.example

kubectl create secret generic postgres-env -n bminfo \
  --from-literal=POSTGRES_USER=bminfo --from-literal=POSTGRES_DB=bminfo \
  --from-literal=POSTGRES_PASSWORD='<same value as DB_PASSWORD>'

kubectl create secret generic bminfo-object-store -n bminfo \
  --from-literal=S3_ENDPOINT_URL=https://silo-api.spainip.es \
  --from-literal=S3_BUCKET=bminfo-raw \
  --from-literal=S3_ACCESS_KEY_ID='<silo access key>' \
  --from-literal=S3_SECRET_ACCESS_KEY='<silo secret key>'
```

`DB_PASSWORD` and `POSTGRES_PASSWORD` describe the same database role and must
agree. `POSTGRES_PASSWORD` is only consulted when the official image creates an
empty data directory, so on this cluster — which has a populated volume — the
authoritative value is the one stored inside PostgreSQL.

## Rotating

### `APP_SECRET_KEY`

```bash
kubectl patch secret web-env -n bminfo --type merge \
  -p "{\"stringData\":{\"APP_SECRET_KEY\":\"$(python -c 'import secrets; print(secrets.token_urlsafe(64))')\"}}"
kubectl rollout restart deployment/web deployment/collector -n bminfo
```

Impact: every session and every outstanding password-reset link is invalidated.
Users must sign in again. Nothing else changes — the key signs nothing durable.

### `ADMIN_PASSWORD`

`bootstrap_admin` runs on every web start and resets the configured
administrator's password from this value, so patching the secret and rolling the
web deployment *is* the rotation.

```bash
kubectl patch secret web-env -n bminfo --type merge \
  -p "{\"stringData\":{\"ADMIN_PASSWORD\":\"$(python -c 'import secrets; print(secrets.token_urlsafe(24))')\"}}"
kubectl rollout restart deployment/web -n bminfo
```

Impact: the administrator's login changes to the new value. Retrieve it from the
secret rather than typing it into a shell history or a chat window.

### `DB_PASSWORD`

This one is not a single patch, because the role password lives in PostgreSQL
and the application's copy lives in `web-env`.

1. Patch `DB_PASSWORD` in `web-env` **and** `POSTGRES_PASSWORD` in
   `postgres-env` to the new value.
2. `kubectl rollout restart deployment/web deployment/collector -n bminfo`.
   The new pods will not become ready yet — they are trying to authenticate with
   the new password while PostgreSQL still holds the old one. The old ReplicaSet
   keeps serving, because its pooled connections (`DB_CONN_MAX_AGE=60`) remain
   authenticated.
3. `kubectl exec -n bminfo deploy/postgres -- psql -U bminfo -d bminfo -c
   "ALTER ROLE bminfo WITH PASSWORD '<new value>';"`
4. Wait for the rollout to finish.

Existing connections survive the `ALTER`; only new ones need the new password.
Expect a few failed connection attempts in the seconds between steps 3 and 4.

Impact beyond the application: **pgAdmin stores its saved server connection,
including the database password, in its own volume.** Rotating `DB_PASSWORD`
breaks that saved connection until the password is re-entered in the pgAdmin UI
under *Servers → bminfo → Properties → Connection*. `PGADMIN_DEFAULT_PASSWORD`
is pgAdmin's own login password and is unrelated.

### `SMTP_PASSWORD`

Issued by the mail provider. Patch it into `web-env` and roll the web
deployment; registration mail fails closed, so a wrong value shows up
immediately as a failed registration rather than a silent drop.

### Silo object-storage keys

Issued by the Silo installation, not by this cluster. See `OBJECT-STORAGE.md`
for the bucket policy. Rotate by patching `bminfo-object-store` and rolling the
web deployment and the archive CronJob's next run. Archiving skips itself while
the credentials are absent or wrong — it does not delete anything it could not
upload — so a rotation mistake delays archiving instead of losing data.

### `registry-credentials`

A GitHub PAT with `read:packages`, stored as a `dockerconfigjson` Secret. A new
token is created in GitHub, not here:

```bash
kubectl create secret docker-registry registry-credentials -n bminfo \
  --docker-server=ghcr.io --docker-username=<user> --docker-password=<token> \
  --dry-run=client -o yaml | kubectl apply -f -
```

Existing pods keep running with the credential they were started with; the new
one takes effect on the next pod creation.

## Rules

- Never commit a secret value, encrypted or not, and never paste one into an
  issue, a pull request, or a chat window. Retrieve it with `kubectl`.
- `.env` and its variants are gitignored; `.env.example` holds placeholders only.
- CI carries no production secret. Its `APP_SECRET_KEY` is a fixed literal used
  to satisfy the length check during tests.
- A secret created from a command that echoes the value leaves it in shell
  history. Generate into a variable or a `chmod 600` file, and shred the file
  afterwards.
