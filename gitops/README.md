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

Cluster bootstrap, Traefik, other applications, and the Flux source and
Kustomization bindings remain in `spainip/infra`. The three existing
`flux-system/bminfo-{postgres,pgadmin,web}` Kustomizations keep their identities,
decryption key reference (`sops-age`), and existing dependencies. Their source
is `GitRepository/bminfo`, with paths under `gitops/production/apps`.
They additionally depend on `bminfo-storage` for the namespace, registry
credentials, and volumes.

## Persistent data

| Application | PVC in namespace `bminfo` | PV | Existing node path |
| --- | --- | --- | --- |
| PostgreSQL | `v-f5b2c398ce5127fa` | `bminfo-v-f5b2c398ce5127fa` | `/opt/stacks/bminfo/django_postgres_data` |
| pgAdmin | `v-170642bc4e016938` | `bminfo-v-170642bc4e016938` | `/opt/stacks/bminfo/pgadmin_data` |

Both PVs stay bound to `spainip-k3s`, use an empty storage class, and retain
their `Retain` reclaim policy. Namespace, PVs and PVCs retain their
`kustomize.toolkit.fluxcd.io/prune: disabled` annotations. The old
`cutover-storage` and new `bminfo-storage` Kustomizations both have `prune: false`;
the latter also has `deletionPolicy: Orphan`.
These settings allow the existing storage objects to be adopted in place.
Never recreate or rename them as part of a repository move.

The older `infra/infrastructure/storage/bminfo-*` and
`infra/drafts/databases/bminfo` files are inactive historical definitions.
They contain different claim names for the same disk paths. Do not activate
them alongside this deployment.

## Secrets and changes

SOPS ciphertext and recipient metadata are preserved without decrypting or
re-encrypting them. The existing cluster key in `flux-system/sops-age` continues
to decrypt them. Use the root `.sops.yaml` rules when editing encrypted files;
do not edit their plaintext fields without SOPS, because the MAC covers them.
Never commit the age private key, GitHub tokens, or decrypted Secrets.

The public repository is fetched over HTTPS without a GitHub credential.
During the handoff, the source is pinned to the migration commit. After live
verification, removing `spec.ref.commit` in
`infra/clusters/production/bminfo.yaml` makes Flux follow `main` for future
GitOps changes. Alternatively, update that commit explicitly for each promotion.

Validate the manifests before promotion:

```bash
for path in storage apps/postgres apps/pgadmin apps/web; do
  kustomize build "gitops/production/$path" > /dev/null || exit 1
done
```

## Handoff and verification

1. Record the current Flux revisions, Deployment and Pod UIDs, restart counts,
   Secret data hashes, and PV/PVC UIDs and bindings. All three Deployments and
   their Flux Kustomizations must be healthy. Create a PostgreSQL custom-format
   backup and validate its archive listing; retain the globals backup too.
2. Publish this repository's migration commit before updating `infra`.
3. In `infra`, add `GitRepository/bminfo` pinned to that commit and
   `Kustomization/bminfo-storage`. Change only the existing application
   Kustomizations' source, paths, and storage dependency. Remove the moved
   resources from `cutover-storage`, whose pruning must remain disabled.
4. Reconcile the root, new source, storage, then the applications:

   ```bash
   flux reconcile kustomization flux-system --with-source
   flux reconcile source git bminfo
   flux reconcile kustomization cutover-storage
   flux reconcile kustomization bminfo-storage
   flux reconcile kustomization bminfo-postgres
   flux reconcile kustomization bminfo-pgadmin
   flux reconcile kustomization bminfo-web
   ```

5. Verify all four BMInfo Kustomizations are Ready at the destination revision;
   the old storage inventory no longer includes BMInfo, and the new storage
   inventory contains all six objects. Compare the recorded identities,
   Deployment specs, Secret hashes, PVC/PV bindings and pod restart counts.
   Check `/health/`, `/status/`, and pgAdmin's login route over HTTPS.
   Repository validation alone is not evidence of a successful live cutover.

## Rollback

Revert the `infra` handoff commit and reconcile `flux-system` with its source.
That restores the original files and source/path bindings in one commit.
Because `bminfo-storage` uses `deletionPolicy: Orphan` and `prune: false`,
removing that Kustomization leaves its namespace, Secrets, and volumes intact.
The existing application Kustomizations are updated, not deleted. Reconcile
`cutover-storage` and the three application Kustomizations, then repeat the
identity and health checks. Keep the destination commit available until the
rollback is verified. No database restore is part of a GitOps-only rollback.
