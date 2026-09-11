# Production GitOps

`production/` is the authoritative BMInfo Kubernetes configuration, migrated
from `spainip/infra` at `828946965797130880a125c4f97e07127cf47b5d`.
The workload and encrypted files were copied byte for byte. This handoff
does not upgrade images, change configuration, run migrations, or move data.

| Directory | Existing resources |
| --- | --- |
| `production/apps/postgres` | PostgreSQL Deployment, environment Secret and Service |
| `production/apps/pgadmin` | pgAdmin Deployment, environment Secret, Service and IngressRoute |
| `production/apps/web` | Django Deployment, environment Secret, Service and IngressRoute |
| `production/storage` | BMInfo Namespace, two retained PV/PVC pairs and encrypted registry Secret |

Cluster bootstrap, ArgoCD, Traefik, and other applications remain in
`spainip/infra`. ArgoCD manages this repository as the `bminfo` Application,
using the paths under `gitops/production/apps` and `gitops/production/storage`.
The ArgoCD SOPS config-management plugin decrypts encrypted files at render
time using the cluster's private age key; plaintext secrets are never stored
in Git.

## Persistent data

| Application | PVC in namespace `bminfo` | PV | Existing node path |
| --- | --- | --- | --- |
| PostgreSQL | `v-f5b2c398ce5127fa` | `bminfo-v-f5b2c398ce5127fa` | `/opt/stacks/bminfo/django_postgres_data` |
| pgAdmin | `v-170642bc4e016938` | `bminfo-v-170642bc4e016938` | `/opt/stacks/bminfo/pgadmin_data` |

Both PVs stay bound to `spainip-k3s`, use an empty storage class, and retain
their `Retain` reclaim policy. These settings allow the existing storage
objects to be adopted in place without changing their identities.
Never recreate or rename them as part of a repository move.

The older `infra/infrastructure/storage/bminfo-*` and
`infra/drafts/databases/bminfo` files are inactive historical definitions.
They contain different claim names for the same disk paths. Do not activate
them alongside this deployment.

## Secrets and changes

SOPS ciphertext and recipient metadata are preserved without decrypting or
re-encrypting them. The existing cluster age key is copied to the `argocd`
namespace for the ArgoCD SOPS config-management plugin. Use the root
`.sops.yaml` rules when editing encrypted files; do not edit their plaintext
fields without SOPS, because the MAC covers them.
Never commit the age private key, GitHub tokens, or decrypted Secrets.

The public repository is fetched over HTTPS without a GitHub credential.
ArgoCD follows the `main` branch for future GitOps changes.

Validate the manifests before promotion:

```bash
for path in storage apps/postgres apps/pgadmin apps/web; do
  kustomize build "gitops/production/$path" > /dev/null || exit 1
done
```

## ArgoCD verification

1. Verify the `bminfo` ArgoCD Application is `Synced` and `Healthy`.
2. Compare the existing Deployment specs, Secret hashes, PVC/PV bindings and
   pod restart counts after adoption.
3. Check `/health/`, `/status/`, and pgAdmin's login route over HTTPS.
   Repository validation alone is not evidence of a successful live cutover.

## Rollback

Suspend or delete the `bminfo` ArgoCD Application before restoring any
application resources manually. Keep the namespace, Secrets, PVs and PVCs;
their retained identities are the rollback boundary. No database restore is
part of a GitOps-only rollback.
