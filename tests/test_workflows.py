import pytest

from repoutils import workflows


@pytest.mark.parametrize(
    ("text", "jobs"),
    [
        ("on: push\n", []),
        ("jobs: {}\n", []),
        ("jobs:\n  a:\n    environment:\n      url: x\n", [("a", workflows.UNKNOWN)]),
        ("jobs:\n  a:\n    environment: 'prod'\n  b: {}\nafter:\n  c:\n", [("a", "prod"), ("b", workflows.UNKNOWN)]),
        ("jobs:\n\n  a:\n    steps:\n      - environment: no\n", [("a", None)]),
        # Quoted keys are YAML too.
        ('jobs:\n  a:\n    environment: {"name": "staging"}\n', [("a", "staging")]),
        ("jobs:\n  a:\n    environment:\n      'name': prod\n", [("a", "prod")]),
        ("jobs:\n  a:\n    environment: {\"name': x}\n", [("a", workflows.UNKNOWN)]),
        # A quoted name may hold a comma, and a quoted job id is a job's.
        ("jobs:\n  'a':\n    environment: {name: \"prod,us\", url: x}\n", [("a", "prod,us")]),
        ("jobs:\n  \"a\":\n    environment:\n      name: 'prod,us'\n      url: x\n", [("a", "prod,us")]),
        # Quotes escaped inside a quoted name are decoded.
        ("jobs:\n  a:\n    environment: {name: 'prod''us'}\n", [("a", "prod'us")]),
        ('jobs:\n  a:\n    environment: {name: "pr\\"od", url: x}\n', [("a", 'pr"od')]),
        ("jobs:\n  a:\n    environment: 'it''s'\n", [("a", "it's")]),
        ('jobs:\n  a:\n    environment: "\\x41"\n', [("a", workflows.UNKNOWN)]),
        # Only the mapping's own name key counts, not one inside a quoted value.
        (
            "jobs:\n  a:\n    environment: {url: \"https://x/?q=, name: 'prod'\", name: staging}\n",
            [("a", "staging")],
        ),
        (
            "jobs:\n  a:\n    environment:\n      url: 'https://x/?name: prod'\n      name: staging\n",
            [("a", "staging")],
        ),
        # An escaped quote doesn't end a quoted name, so a # after it isn't a comment.
        ('jobs:\n  a:\n    environment: "pr\\"od # x"\n', [("a", 'pr"od # x')]),
        # Only a block mapping's direct entries count, not lines in an entry's value.
        (
            "jobs:\n  a:\n    environment:\n      url: >-\n        x\n        name: prod\n      name: staging\n",
            [("a", "staging")],
        ),
        ("jobs:\n  a:\n    environment:\n      url: x\n\n      name: staging\n", [("a", "staging")]),
        # A name this can't read is unknown: a flow mapping over several lines, an
        # alias, a block scalar or a collection.
        ("jobs:\n  a:\n    environment: {name: prod,\n      url: x}\n", [("a", workflows.UNKNOWN)]),
        ("jobs:\n  a:\n    environment: *env\n", [("a", workflows.UNKNOWN)]),
        ("jobs:\n  a:\n    environment:\n      name: |\n        prod\n", [("a", workflows.UNKNOWN)]),
        ("jobs:\n  a:\n    environment: {name: [prod]}\n", [("a", workflows.UNKNOWN)]),
        # An & or ! after the start of a value is part of it, not an anchor or tag.
        ("jobs:\n  a:\n    environment: {name: staging, url: 'https://x/?a=1&b=2'}\n", [("a", "staging")]),
        ("jobs:\n  a:\n    environment: r&d!\n", [("a", "r&d!")]),
        # An anchor or tag on a name is skipped.
        ("jobs:\n  a:\n    environment: &e !!str prod\n", [("a", "prod")]),
        ("jobs:\n  a:\n    environment: &e\n      name: !!str prod\n", [("a", "prod")]),
        # An anchor or tag doesn't make a job a flow mapping, but what follows it can.
        ("jobs:\n  a: &a\n    environment: prod\n  b: !!map {}\n", [("a", "prod"), ("b", workflows.UNKNOWN)]),
        # A line that isn't a job's id starts no job.
        ("jobs:\n  - a\n  b:\n", [("b", None)]),
    ],
)
def test_jobs(text: str, jobs: list[tuple[str, str | None]]) -> None:
    lines = [workflows.without_comment(line) for line in text.splitlines()]
    assert [(job.id, job.environment) for job in workflows.jobs("w.yml", lines)] == jobs


@pytest.mark.parametrize(
    ("text", "job_level"),
    [
        ("jobs:\n  a:\n    if: vars.A\n    steps:\n      - if: vars.B\n", {"A"}),
        ("jobs:\n  a:\n    runs-on: >-\n      ${{ vars.A }}\n    env:\n      X: ${{ vars.B }}\n", {"A"}),
        # Only an environment's name counts, not its url, which is evaluated when the job ends.
        (
            "jobs:\n  a:\n    environment:\n      url: >-\n        ${{ vars.B }}\n      name: ${{ vars.A }}\n",
            {"A"},
        ),
        ("jobs:\n  a:\n    environment: {name: '${{ vars.A }}', url: \"${{ vars.B }}\"}\n", {"A"}),
        ("jobs:\n  a:\n    environment: ${{ vars.A }}\n", {"A"}),
        # A flow mapping may go on over several lines, with its url in any of them.
        ("jobs:\n  a:\n    environment: {url: ${{ vars.B }},\n      name: ${{ vars.A }}}\n", {"A"}),
        ("jobs:\n  a:\n    environment: {name: ${{ vars.A }},\n      url: ${{ vars.B }}}\n", {"A"}),
        # A block sequence may sit at its key's indent.
        ("jobs:\n  a:\n    runs-on:\n    - ${{ vars.A }}\n    steps:\n    - run: ${{ vars.B }}\n", {"A"}),
        ("jobs:\n  a:\n    steps:\n      - name: ${{ vars.A }}\n", set()),
        ("jobs:\n  a: {if: vars.A}\n", set()),
    ],
)
def test_job_level_variables(text: str, job_level: set[str]) -> None:
    lines = [workflows.without_comment(line) for line in text.splitlines()]
    (job,) = workflows.jobs("w.yml", lines)
    assert job.job_level_variables == job_level


def test_job_level_uses_name_the_key() -> None:
    text = "jobs:\n  a:\n    if: vars.A\n    name: ${{ vars.A }} ${{ vars.B }}\n"
    (job,) = workflows.jobs("w.yml", text.splitlines())
    assert job.job_level_uses == {("if", "A"), ("name", "A"), ("name", "B")}
