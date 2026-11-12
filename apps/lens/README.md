# `llmd`

`llmd` plans, stands up, rolls out, and removes [llm-d](https://github.com/llm-d/llm-d) guides inside one
existing Kubernetes or OpenShift namespace. It does not need cluster-admin rights, and it does not create
namespaces, CRDs, cluster roles, PersistentVolumes, SCCs or network attachments: it reports those for an
administrator.

A *stack* is what a team deploys into one namespace:

- an llm-d guide, such as `guides/optimized-baseline`. `llmd` reads the guide's `guide.yaml`, the file the
  guide's README commands are generated from, and follows its `deploy` steps without running a shell. It
  renders the router chart with `helm template`, using the same chart, version and values as the guide's
  `helm install`, and renders each model server overlay with `kustomize build`;
- one kustomize directory, such as one model server overlay of a guide;
- manifest files, read as they are.

An *instance* is a stack running in a namespace. It is recorded in a ConfigMap `lens-instance-<name>` in that
namespace, so every command works from any machine. The record's fields and phases, and how `llmd` maps onto the
Lens roadmap, are described in the [Lens CLI design](../../docs/design/lens-cli-design.md).

`llmd` is part of the Lens engine package (`apps/lens`). It is pure Python and depends only on PyYAML. It
does not need the Lens service, its database or Node.

## Install

From the repository root, in any Python 3.11+ virtual environment:

```sh
python3 -m venv .venv
.venv/bin/pip install -e ./apps/lens
.venv/bin/llmd --version
```

`llmd` runs `kubectl` (or `oc`, with `--kube-cli oc`), `helm`, `git`, and `kustomize` (or `kubectl kustomize`
when kustomize is not installed). Install them on the `PATH`.

## Sources

A guide comes from an llm-d checkout, or from upstream directly:

```sh
llmd sources add llm-d https://github.com/llm-d/llm-d.git#main
llmd stacks                           # the guides, then the other kustomize directories
llmd stacks optimized-baseline        # one guide's variables, support matrix and steps
```

`llmd stacks` lists two groups: the guides (directories with a `guide.yaml`), and the kustomize directories that
no other directory includes, outside the guides, Helm charts and kustomize Components. A stack is named by its
path, or by the end of its path when only one listed stack ends that way: `optimized-baseline` and
`models/deepseek-v4` are enough for `guides/optimized-baseline` and `guides/models/deepseek-v4`.

A checkout of your own team's stacks is a source too. Say which of its kustomize directories are stacks when you
register it, so the listing and the short names cover those only:

```sh
llmd sources add team ~/src/team-stacks --stacks 'deploy/*/*' --exclude 'deploy/**/base'
```

`*` and `?` match within one path segment, and `**` matches any number of segments. The patterns are kept with
the source in `sources.yaml`. Guides are always listed.

`--source` takes a registered name, a local path, or `URL#REF`. REF can be a branch, a tag, a commit, or any
ref the remote serves, so a pull request can be planned before it merges:
`--source https://github.com/llm-d/llm-d.git#refs/pull/<N>/head`. With exactly one registered source,
`--source` may be left out. A remote checkout is cached; `--refresh` fetches it again.

The namespace comes from `-n`, else `$LLMD_NAMESPACE`, else the kube context.

## Guide variables

A guide's README sets variables with `export` lines (`ACCELERATOR_TYPE`, `MODEL_SERVER`, `INFRA_PROVIDER`, and
the guide's own). `--var NAME=VALUE` sets them for `llmd`. The guide's `when:` filters then pick the same steps
the README would run:

```sh
llmd plan optimized-baseline -n <namespace> --var INFRA_PROVIDER=ocp --offline
```

`--set KEY=VALUE` passes a value to the Helm charts the guide installs (the router), as `helm install --set`
would. `NAMESPACE` comes from `-n`, and `REPO_ROOT` is the source checkout. The plan lists each guide step and
what `llmd` did with it:

- `followed`: the step's `helm install` or `kubectl apply -k|-f` is rendered.
- `yours`: creating the namespace or a Secret is left to you. The plan checks that the Secret exists.
- `administrator`: a cluster-wide prerequisite (CRDs from the network, an operator).
- `other variant`: the step applies a directory this checkout does not have for these variables.
- `not followed`: `llmd` cannot reproduce the step (a pipe through `sed` or `envsubst`, a script). The plan is
  blocked, unless the guide's own CI skips that step too (`skip_in: [ci]`).

Gateway Mode needs a cluster Gateway, so `llmd` follows Standalone Mode.

## An empty namespace

```sh
llmd plan optimized-baseline -n <namespace>       # read-only: findings, prerequisites, a server dry run
llmd standup optimized-baseline -n <namespace> --expected-digest <digest from the plan> --wait
llmd status optimized-baseline -n <namespace>
llmd teardown optimized-baseline -n <namespace>
```

`plan` renders the guide, moves every object into the namespace, and reports what blocks a standup:

- objects only an administrator may apply. `--admin-yaml FILE` writes them; pass `--skip-admin-parts` once
  they are applied;
- privileged containers and hostPath volumes. Pass `--allow-privileged` once an administrator has granted them;
- missing Secrets and claims, missing permissions, and objects another tool already deployed;
- a claim that is being deleted, or Lost. Pods that already mount it keep running, but a new pod cannot
  mount it, so it stays Pending. `scale` and `rollouts` refuse for the same reason.

The llm-d router chart creates a ClusterRole and ClusterRoleBinding so that the endpoint picker can check who
scrapes its metrics. Without an administrator, `--set router.monitoring.prometheus.auth.enabled=false` turns
that check off. The metrics are then served without authentication inside the cluster.

`standup` records the instance before it applies anything, then applies with server-side apply. It also
records the `--var` and `--set` values with the instance. `teardown` deletes exactly the recorded objects whose
UID still matches. Claims are kept unless you pass `--delete-volumes`.

## Companions

Some teams keep parts of a namespace apart from the model stack on purpose, so that tearing the model down
leaves them: NetworkPolicies for the namespace, or a model-cache claim whose volume an administrator binds once.
`llmd` calls each such part a *companion*. Say where a source keeps them when you register it, as path templates
relative to the checkout:

```sh
llmd sources add team ~/src/team-stacks \
  --companion 'deploy/namespaces/{name}/policies/{namespace}' \
  --companion '{stack}/../base/model-cache-pvc.yaml'
llmd stacks deploy/model-a/cluster-a --companions <namespace>   # what the templates find
```

`{stack}` is the stack's path, `{name}` its last segment and `{namespace}` the target namespace; `..` steps up a
directory and `*` matches within one segment. A template that matches nothing is skipped, and one that matches
several paths is reported with all of them. Each match is a kustomize directory or a manifest file.

`plan` reports each companion the namespace lacks. `standup --with-companions` stands those up first, in the
order the templates were given, each as its own instance (`<last segment>-companion`). A teardown of the stack
keeps them. For a claim that is missing or Pending, `plan` also names the file in the source that defines its
PersistentVolume, for an administrator to apply; `llmd` neither reads nor writes PersistentVolumes.

## A guide already deployed with its README's commands

```sh
llmd import optimized-baseline -n <namespace> --name <instance> --dry-run   # do the running objects match?
llmd import optimized-baseline -n <namespace> --name <instance>
```

`import` records the running objects by UID and changes nothing else: no labels, no apply, and no field
ownership, so the team's own `kubectl apply -k` and `helm upgrade` keep working. `--dry-run` compares the
running objects with the guide's render first. `llmd teardown <instance> --forget` stops managing the instance
and leaves every object running.

## Scaling

```sh
llmd scale <instance> -n <namespace> optimized-baseline-nvidia-gpu-vllm-decode=1
llmd scale <instance> -n <namespace> pd-disagg-vllm/decode=2 --no-drain     # a DisaggregatedSet role
llmd scale <instance> -n <namespace> optimized-baseline-nvidia-gpu-vllm-decode=--   # back to the files
```

`llmd scale` sets the replicas of a Deployment, StatefulSet or LeaderWorkerSet, or of a DisaggregatedSet role
(`SET/ROLE`). It applies only that object and keeps the count in the instance's record, so a later `standup` or
`rollouts` keeps it rather than setting it back to what the guide says, which a plain `kubectl scale` would not
survive. `plan` and `status` list each kept count. `WORKLOAD=--`, or the count the files say, clears it. A guide
is rendered with the `--var` and `--set` values the instance was stood up with.

Lowering a DisaggregatedSet role removes its highest groups. `--no-drain` lowers it directly: the guides' vLLM
pods drain as they stop. A stack that admits the lowering only from its own drain tool passes it with
`--drain COMMAND` (`{workload}`, `{role}`, `{set}`, `{count}`, `{namespace}`, `{source}`); llmd runs it, waits,
and records the count once it exits 0. `--drain` works for a Deployment too; without it a Deployment is lowered
directly. Scaling to 0 asks first (`-y` skips). `--dry-run` sends the change as a server dry run. A rollout in
progress refuses a scale.

`--drain-idle` drains the pods without a tool of the stack's own, for a stack whose InferencePool selects
serving pods by a label (`--pool-label`, "true" while a pod serves):

```sh
llmd scale <instance> -n <namespace> my-model=12 --drain-idle --batch 2 --pool-label <label>
```

llmd picks the pods (a Deployment's Ready pods with the fewest requests; a role's highest groups), sets the pool
label to `false`, and waits until every vLLM engine on them is idle: no running or waiting request and no KV
cache in use on several reads, then no finished request during a grace period. It reads `/metrics` through the
API server's pod proxy (`get` on `pods/proxy`; `--metrics-port` when the containers do not declare the port).
For a Deployment it then sets the lowest `controller.kubernetes.io/pod-deletion-cost` on those pods, lowers the
count, and checks that the controller removed them and no others. `--batch N` drains N at a time;
`--drain-timeout` (default 1800 s) puts a batch back in the pool when it does not go idle. It refuses when an
InferencePool selects the pods without the pool label, when another pod of a Deployment is not Ready (its
ReplicaSet would remove that one first), and during a rollout. The record keeps each count reached.

`--wait` waits until each raised workload serves: every pod of a replica Ready and, with a pool label, in the
pool (`--timeout`, default 7200 s).

## Labels on running pods

```sh
llmd label <instance> -n <namespace> my-model example.com/router=pd example.com/role=both \
  --annotate llm-d.ai/active-ports=8000
llmd label <instance> -n <namespace> my-model example.com/role-       # remove one
```

`llmd label` patches the running pods of a workload (`NAME` or `SET/ROLE`) without a restart, and keeps the
labels in the record. A router that takes over can then select pods that already run, with their caches.
`--annotate` sets annotations the router reads (such as `llm-d.ai/active-ports`, the pool target ports a pod
serves) in the same patch, so a pod never joins a pool without them. Pods created later start without either:
`llmd scale --wait` gives them to a raised workload's new pods, and `llmd status` names running pods that lack
them. Keys the controller selects pods by, the labels lws keeps, the pool label and the pod deletion cost are
refused.

## Moving an instance to another namespace or cluster

```sh
llmd migrate plan   <instance> --from <namespace-a> --to <namespace-b>     # read-only
llmd migrate start  <instance> --from <namespace-a> --to <namespace-b>     # the target with the workload at 1
llmd migrate step   <instance> --from <namespace-a> --to <namespace-b>     # repeat until the source is at 0
llmd migrate status <instance> --from <namespace-a> --to <namespace-b>
llmd migrate finish <instance> --from <namespace-a> --to <namespace-b>     # tears the source down
```

`migrate` moves one workload (by default the stack's only scalable one that asks for accelerators; else
`--workload`) a replica at a time, so capacity does not drop:

- `plan` checks both sides, read-only: the source is settled and not rolling, the target's plan (with its
  companions), and that the target has no other instance of that name.
- `start` stands the stack up in the target with the workload at 1 (a `scale` count; `--with-companions` first)
  and marks the migration in both records. The target is rendered with the source's guide variables (`--var`,
  `--set`), and `--stack` gives it another stack path.
- `step` raises the target by one, waits until its new replica is Ready (`--timeout`), and only then lowers the
  source by one, through `--no-drain` or `--drain` for a DisaggregatedSet role, as for `scale`. A step that
  times out leaves the source as it was.
- `finish` tears the source down once it is at 0, keeping its claims.

Traffic is moved by hand between steps (a gateway's route weights, or the like); `llmd` does not change it. Each
side has its own `--from-context`/`--to-context`, `--from-kubeconfig`/`--to-kubeconfig` and
`--from-source`/`--to-source`, so the same steps move an instance to another cluster.

## Rollouts

A rollout moves an instance to what another ref of the same stack renders, in place. In a guide whose model
server is one DisaggregatedSet (`guides/pd-disaggregation`, `guides/wide-ep`), a change to the pod templates
makes its controller (lws) replace every group. With `maxSurge: 0` and `maxUnavailable: N` per role, each step
removes up to N old groups of every role and creates as many new ones.

```sh
NEW=https://github.com/llm-d/llm-d.git#refs/pull/<N>/head
llmd rollouts plan pd-disaggregation -n <namespace> --name <instance> --source "$NEW"
llmd rollouts start pd-disaggregation -n <namespace> --name <instance> --source "$NEW" --no-drain --watch
```

`rollouts plan` is read-only. It shows the objects that change, whether the DisaggregatedSet rolls or only
scales, the steps, and the groups each role keeps serving during a step. It refuses a rollout in these cases:

- a roll is in progress, or a group is not serving;
- several DisaggregatedSets would roll at once;
- roles are added or removed;
- a step would leave a role below its `--floor`;
- a step would leave a role with no group serving. `--allow-outage` accepts that outage;
- the stack path differs from the one the instance runs. `--switch-stack` rolls it to that path instead,
  for example from the overlay a team runs to the copy it prepared a change in. The record then follows
  the new path, and keeps the old one as `previousStack`.

The llm-d guides' vLLM overlays drain each pod as it stops: a `preStop` sleep, then vLLM's
`--shutdown-timeout`. `--no-drain` relies on that. A stack that holds the controller's scale-down until a drain
tool takes a group out of the pool passes that tool with `--drain`. `llmd` runs it once per role, filling in
`{role}`, `{source}`, `{namespace}`, `{set}`, `{old_revision}` and `{max_unavailable}`. Each drainer's output is
under `$LENS_DATA_DIR/llmd/rollouts/<namespace>/<instance>/`.

`--pool-label LABEL` names the pod label such a tool sets to `false` when it takes a group out of the pool; a
group then counts as serving only while that label is `true`. Register it once with
`llmd sources add NAME PATH --pool-label LABEL`. `rollouts start` records the label it used, so `status`,
`--watch`, `abort` and `finish` judge groups the same way without the flag.

```sh
llmd rollouts status <instance> -n <namespace> [--watch]   # per role and revision; the drainers
llmd rollouts pause <instance> -n <namespace>               # stop the drainers
llmd rollouts resume <instance> -n <namespace> [--watch]    # start them again, on the same machine
llmd rollouts abort <instance> -n <namespace> [--watch]     # re-apply what ran before; it rolls back the same way
llmd rollouts finish <instance> -n <namespace>              # end it once the old revision is gone
```

A change to a Deployment's or StatefulSet's pod template (a router's endpoint picker, say) replaces its pods.
`rollouts plan` warns about each one (`pods-restart`: how many pods, and what a surge needs), and the rollout
stays `Rolling` until the new pods are Ready and no old pod is left, so `status --watch`, `abort` and `finish`
follow it like a DisaggregatedSet roll. A change that replaces no pods finishes at once.

A rollout never deletes. What the instance's stack no longer renders keeps running until pruned:

```sh
llmd rollouts prune <instance> -n <namespace> --dry-run      # what would go, and why the rest stays
llmd rollouts prune <instance> -n <namespace>                # asks first; -y skips
```

`prune` renders the stack at the revision the instance runs and deletes a recorded object it no longer renders
once nothing uses it: a workload at 0 replicas with no pod left; a ConfigMap, Secret or ServiceAccount no pod
refers to. Claims stay unless `--delete-volumes`. A ref that only drops objects is recorded by `rollouts start`
(nothing to apply), so `prune` can follow it.

## Development

```sh
cd apps/lens
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
.venv/bin/ruff check lens tests
.venv/bin/ruff format --check lens tests
```

The tests use an in-memory cluster (`tests/fakes.py`). It answers the kubectl calls `llmd` makes and acts as
the Deployment and lws controllers. Git, kustomize and Helm run for real, against a small checkout in llm-d's
layout (`tests/data/llmd`).
