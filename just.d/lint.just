# Static checks across the surfaces ty / yamllint / go vet cover.
# Run `just lint` before committing.
#
#   just lint             aggregate (lint-yaml lint-python lint-go)
#   just lint-yaml        yamllint (config/ansible/**/*.yml + root yamls)
#   just lint-python      ty (admin-api + ycluster package + contrib/)
#   just lint-go          go vet on local-ai-proxy
#   just lint-ansible     ansible-lint (config/ansible; no inventory needed)

# yamllint default rules are too strict (line-length 80, document-start
# required, indentation style mandated) for the codebase as it stands;
# relax them to "syntax + key hygiene" — yamllint still catches syntax
# errors, duplicate keys, broken indentation, mismatched braces/brackets.
# Trailing-whitespace is left to editor tooling (lots of legacy in-tree).
# Override with `YAMLLINT_EXTRA_ARGS=...`.
export YAMLLINT_ARGS := "-d '{extends: relaxed, rules: {line-length: disable, indentation: disable, comments-indentation: disable, comments: disable, truthy: disable, braces: disable, octal-values: disable, empty-lines: disable, empty-values: disable, quoted-strings: disable, trailing-spaces: disable}}' " + env_var_or_default('YAMLLINT_EXTRA_ARGS', '')

lint: lint-yaml lint-python lint-go lint-ansible

lint-yaml:
    yamllint {{YAMLLINT_ARGS}} \
        config/ansible \
        config.yml docker-compose.yaml opencode.json \
        pyrightconfig.json
    @test -f secrets.yml && yamllint {{YAMLLINT_ARGS}} secrets.yml || true

lint-python:
    @if command -v ty >/dev/null 2>&1; then ty check; else echo "ty not installed; skipping lint-python (install with 'just setup-tools')"; fi

lint-ansible:
    @test -x .venv/bin/ansible-lint || { echo 'ansible-lint not installed; run just setup-venv'; exit 1; }
    @mkdir -p ./tmp/ansible ./tmp/ansible-tmp
    @ANSIBLE_HOME="$PWD/tmp/ansible" ANSIBLE_LOCAL_TMP="$PWD/tmp/ansible-tmp" TMPDIR="$PWD/tmp" \
        .venv/bin/ansible-lint --profile min --offline --nocolor \
        --exclude config/ansible/vault --exclude 'config/ansible/*/files' config/ansible

lint-go:
    make -C local-ai-proxy vet
