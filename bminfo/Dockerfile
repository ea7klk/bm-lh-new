# python:3.14-slim, pinned by index digest.
#
# Resolved 2026-11-13: Python 3.14.10, image built 2026-11-05, multi-arch index.
# The digest is the index, not a per-architecture manifest, so an arm64 build
# still resolves to the arm64 image.
#
# Pinned on purpose. `python:3.14-slim` is a floating tag: it becomes a new
# image whenever upstream rebuilds, which changes the base OS and the Python
# patch level with no commit to show for it. With the digest, a base-image change
# is a reviewable diff. To move it, re-resolve the digest and update both the
# tag and this comment -- DEPENDENCIES.md has the command.
FROM python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f

WORKDIR /app

# requirements.lock is the install input, not requirements.txt. The lock is
# generated from requirements.txt by pip-compile with --generate-hashes, so it
# pins every transitive package and every wheel hash. --require-hashes makes pip
# refuse any distribution whose hash does not match, which is what makes "CI
# tested this" and "production runs this" the same statement.
COPY bminfo/requirements.txt ./requirements.txt
COPY bminfo/requirements.lock ./requirements.lock
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir --root-user-action=ignore --require-hashes -r requirements.lock

COPY bminfo/ ./

RUN chmod +x entrypoint.sh
CMD ["./entrypoint.sh"]
