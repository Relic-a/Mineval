# VPS execution

Mineval uses the local store and dashboard as the authoritative record. The worker
is reached only over SSH; it opens no HTTP, Docker API, or dashboard listener.
SSH host keys must already be trusted and non-interactive public-key authentication
must work. The remote account must be trusted with the selected private task evidence
and model-only Codex credential. Existing Web Arena execution stays local.

Configure a worker, install the current Mineval package into the remote user's
private directory, and provision one model-only credential file:

```sh
mineval vps configure deploy@78.46.108.158
mineval vps install
mineval vps provision-auth --auth-file "$HOME/.codex/auth.json"
mineval vps health --image YOUR_PRELOADED_IMAGE
```

The installer refuses to replace worker code while its coordinator is active. It
does not install operating-system packages or start Docker. The credential is
validated through the existing Codex auth provisioner and stored with mode 0600
inside a mode 0700 worker directory. It is sent over SSH separately from job data.
Use a dedicated account and protect its SSH keys. Provision again after credential
rotation. Configure trusted runtime images and evaluation settings on the local
launcher as for local work. The rootless daemon on the VPS must contain the same
preloaded images, including the exact pinned image IDs in frozen revisions. Check
each required ID with `mineval vps health --image sha256:...`. Images
are not pulled, built, or copied automatically.

Choose **Local** or **VPS** on the preparation and benchmark confirmation pages.
Activity displays the host, remote stage and errors. A remote submission uses a
stable job ID and an immutable input bundle. After a lost connection the local
worker queries that ID; it does not create another attempt. A remote coordinator
uses the existing host-side admission and resource measurements, and individual
job execution stores are separate from the local authoritative store. A completed
result is checksum-validated and imported under the local task lock. Conflicting
local evidence is rejected. Uploads resume from a confirmed offset and the worker
binds each submission to its immutable archive receipt before accepting data. An
older, incomplete submission without a recorded bundle identity cannot be resumed
automatically; inspect it before any manual recovery. Downloads reuse one SSH
connection, resume by offset, and discard a descriptor when the remote attempt or
evaluation changes. Export archives are deleted after import; abandoned exports
expire after 24 hours, and a private background reaper removes them. Expired exports
are recreated on the next download. Retrying a failed job is explicit. The worker account
holds private task data and job evidence until manually removed; retain it while
reconciliation may still be necessary.

When the VPS seals a functional grade, Mineval imports it into Activity while
the optional qualitative reviewer can still be running. The final import brings
back qualitative observations and resource usage. With `architecture_review`
enabled, the local worker reviews the imported attempt against available
architecture records for the same revision. This review runs separately from
grade delivery. Early grades require the current package on both hosts.

## Target host prerequisites

The target was inspected with Debian 13, Docker CE 29.5.2, cgroup v2, user systemd,
unprivileged user namespaces and subordinate ID ranges for `deploy`. It also has
a separate **rootful** system Docker daemon, which Mineval refuses. The administrator
has since installed and enabled a separate rootless daemon; the pinned
`mineval-gretel:node24` image was copied to it and its image ID was verified.
For a fresh host with the same package versions, an administrator should first
simulate the following and inspect the proposed changes; the original simulation
reported five new packages and no upgrades or removals:

```sh
sudo apt-get -s install uidmap slirp4netns \
  'docker-ce-rootless-extras=5:29.5.2-1~debian.13~trixie'
```

If the simulation still reports no unexpected changes, an administrator can run:

```sh
sudo apt-get install uidmap slirp4netns \
  'docker-ce-rootless-extras=5:29.5.2-1~debian.13~trixie'
sudo loginctl enable-linger deploy
```

Do not install Debian's separate `rootlesskit` package alongside Docker CE's
rootless extras: these packages conflict on this host. As `deploy`, in its user
session, install and start the rootless user daemon while leaving the system daemon
running:

```sh
dockerd-rootless-setuptool.sh install --force
systemctl --user enable --now docker.service
```

Docker's setup tool changes the user's default Docker CLI context to `rootless`;
restore the previous context with `docker context use default` if other existing
workflows depend on it. Mineval explicitly uses `/run/user/1000/docker.sock` for
this account, independently of the default context. Check `mineval vps health`
afterwards. The Docker documentation explains the separate rootless user daemon:
https://docs.docker.com/engine/security/rootless/.

Remote preparation imports the task evidence and draft checkpoints, including
failed or interrupted work, after checking them against the transfer baseline.
If publication is interrupted, task operations stay blocked until the same import
is retried. Failed or interrupted work remains available for explicit retry;
it is not automatically replayed. A failed transfer or a local evidence conflict
is reported as interrupted and leaves remote evidence intact. Live execution also
requires the matching rootless images and is not established by SSH health alone.

The remote coordinator reads `resources.reservation_percent` from its private worker
`settings.json` (default `80`, greater than zero and at most `100`). It reserves that
percentage of the job's Docker CPU and memory limits for each expected container,
then accounts for live measurements and memory peaks. Preparation accounts for its
agent and broker. These are admission estimates; the Docker hard limits do not change.
The local scheduler continues to use its configured per-job estimates.
