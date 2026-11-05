import json

import pytest

from lens.catalog import default_name, find_stack, list_stacks, matches
from lens.cli import main
from lens.errors import NotFoundError, UsageError
from lens.process import subprocess_runner
from lens.sources import resolve_source

GUIDES = ["guides/demo-baseline", "guides/demo-pd"]
#: Kustomize directories no other one includes, outside the guides. The recipe base is included by a
#: guide's overlay and the Component is never applied on its own, so neither is listed.
KUSTOMIZE = ["examples/model-a/cluster-a", "examples/staged/model-a/cluster-a"]


def ids(stacks):
    return [stack.id for stack in stacks]


def test_guides_then_the_other_kustomize_directories(llmrepo):
    source = resolve_source(str(llmrepo))
    stacks = list_stacks(source, subprocess_runner)
    assert ids(stacks) == GUIDES + KUSTOMIZE
    assert [stack.kind for stack in stacks] == ["guide"] * 2 + ["kustomize"] * 2


def test_listing_by_prefix_and_by_pattern(llmrepo):
    source = resolve_source(str(llmrepo))
    assert ids(list_stacks(source, subprocess_runner, "examples")) == KUSTOMIZE
    assert ids(list_stacks(source, subprocess_runner, "guides/demo-pd/")) == ["guides/demo-pd"]
    narrowed = list_stacks(source, subprocess_runner, include=("examples/*/*",))
    assert ids(narrowed) == [*GUIDES, "examples/model-a/cluster-a"]  # guides are always listed
    deep = list_stacks(source, subprocess_runner, include=("examples/**/cluster-a",), exclude=("**/staged/**",))
    assert ids(deep) == [*GUIDES, "examples/model-a/cluster-a"]


def test_globs_stay_within_a_segment_unless_doubled():
    assert matches("examples/model-a/cluster-a", "examples/*/*")
    assert not matches("examples/staged/model-a/cluster-a", "examples/*/*")
    assert matches("examples/staged/model-a/cluster-a", "examples/**/cluster-a")
    assert matches("examples/model-a/cluster-a", "examples/**/cluster-a")
    assert not matches("examples/model-a", "examples/**/cluster-a")


def test_a_stack_is_a_guide_or_any_path_with_manifests(llmrepo):
    source = resolve_source(str(llmrepo))
    assert find_stack(source, "guides/demo-baseline").kind == "guide"
    overlay = find_stack(source, "guides/demo-baseline/modelserver/gpu/vllm/base")
    assert overlay.kind == "kustomize"
    assert find_stack(source, "examples/model-a/cluster-a").kind == "kustomize"
    plain = find_stack(source, "examples/model-c/plain/")
    assert (plain.id, plain.kind) == ("examples/model-c/plain", "manifests")
    single = find_stack(source, "examples/model-c/plain/deployment.yaml")
    assert single.kind == "manifests"
    with pytest.raises(NotFoundError):
        find_stack(source, "guides/missing")
    with pytest.raises(UsageError, match="escapes"):
        find_stack(source, "../outside")
    with pytest.raises(UsageError, match="no Kubernetes manifests"):
        find_stack(source, "charts/router/Chart.yaml")


def test_a_stack_is_found_by_the_end_of_its_path(llmrepo):
    source = resolve_source(str(llmrepo))
    assert find_stack(source, "demo-baseline", runner=subprocess_runner).id == "guides/demo-baseline"
    assert find_stack(source, "staged/model-a/cluster-a", runner=subprocess_runner).kind == "kustomize"
    with pytest.raises(UsageError, match="names 2 stacks") as refused:
        find_stack(source, "model-a/cluster-a", runner=subprocess_runner)
    assert "examples/model-a/cluster-a, examples/staged/model-a/cluster-a" in refused.value.message
    with pytest.raises(NotFoundError):
        find_stack(source, "demo-baseline")  # without the listing, only a path is found
    with pytest.raises(NotFoundError):
        find_stack(source, "missing", runner=subprocess_runner)


def test_default_names_are_short_dns_labels(llmrepo):
    source = resolve_source(str(llmrepo))
    assert default_name(find_stack(source, "guides/demo-baseline")) == "demo-baseline"
    assert default_name(find_stack(source, "examples/model-a/cluster-a")) == "model-a-cluster-a"
    long = find_stack(source, "examples/model-c/plain")
    long = type(long)(id="examples/" + "x" * 60 + "/cluster.v1.2", kind="kustomize", source=source)
    name = default_name(long)
    assert len(name) <= 40 and name.endswith(name[-8:]) and name.islower()


def test_stacks_command_lists_guides_and_kustomize_directories_apart(llmrepo, tmp_path, capsys):
    state = ["--state-dir", str(tmp_path / "s")]
    assert main([*state, "stacks", "--source", str(llmrepo)]) == 0
    out = capsys.readouterr().out
    assert "Guides (2)" in out and "Kustomize directories (2)" in out
    assert out.index("guides/demo-pd") < out.index("Kustomize directories") < out.index("examples/model-a/cluster-a")
    assert main([*state, "-o", "json", "stacks", "--source", str(llmrepo)]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["guides"] == GUIDES and listed["kustomize"] == KUSTOMIZE


def test_a_registered_layout_narrows_the_listing(llmrepo, tmp_path, capsys):
    state = ["--state-dir", str(tmp_path / "s")]
    add = ["sources", "add", "demo", str(llmrepo), "--stacks", "examples/**", "--exclude", "**/staged/**"]
    assert main([*state, *add]) == 0
    assert "stacks     examples/**" in capsys.readouterr().out
    assert main([*state, "-o", "json", "stacks"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["kustomize"] == ["examples/model-a/cluster-a"] and listed["guides"] == GUIDES
    # The end of a path is looked up among the listed stacks only, so it is no longer ambiguous.
    assert main([*state, "render", "model-a/cluster-a"]) == 0
    assert "Deployment" in capsys.readouterr().out
