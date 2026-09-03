# perp-bot

Perp CLOB market-making keeper + event-replay backtester. Decides WHAT target state to reach
(target inventory, passive vs urgent de-risking, quote prices/sizes) and emits `ExecIntent`s;
it does NOT place orders itself.

## Role

- Belongs to the `amm-solution` monorepo; depends on shared brain `mm_core` (pip install -e).
- Execution is owned by the OPMS exec layer — sees `dex_executor`/OPMS and `hb-enhanced-opms`.
- Consumes OPMS market-data + fill streams, runs AS/regime/risk via mm_core, emits ExecIntent back.

## Rules

- Test Driven Design: write tests first, confirm they FAIL, commit, then implement. One task per loop. Update planning docs, commit after completion.
- Compulsory virtual env: always create/use a dedicated `.venv` (e.g. `python -m venv .venv`). Never install deps into global system Python or borrow another project's env.
- Python 3.11+. `pytest` is the test runner — suites live in `tests/`.
- Keep keeper logic (decisions) free of exchange-native verbs; routing to a venue belongs in the exec layer, not here.
- Editing: prefer `patch` with unique context over `write_file`. Re-read the file first; patch hallucinates old_string often.
- When prompting for selection, list items numbered (1, 2, 3...). Never ask more than one yes/no question.
- Safety-first: reversible actions only (trash > rm). Scientific rigor: verify everything, never guess. Minimalist and lean.
- Direct infra changes (services, configs): ask first, or tell the user the exact sudo command to run.
- Communication: concise, terse, English only. "y" = go ahead — don't second-guess.