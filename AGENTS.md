# Contributor map

This repository is a small, production-minded receipt-processing POC.

Read these before changing behavior:
- [Requirements](docs/REQUIREMENTS.md): scope, acceptance criteria, security, assumptions.
- [Architecture](docs/ARCHITECTURE.md): boundaries, persistence, data model, failure handling.
- [Implementation plan](docs/IMPLEMENTATION_PLAN.md): milestone scope and verification gates.
- [Current status](docs/CURRENT.md): completed work, blockers, and next action.

Work only on the authorized milestone. Keep business logic deterministic and use AI
only for receipt reading and extraction. Do not add a workflow framework, background
infrastructure, frontend, or multiple agents without demonstrated necessity.

Test new behavior at service boundaries with mocked external providers. Run relevant
checks before marking work complete; record actual results and limitations in
`docs/CURRENT.md`. Never commit credentials, receipt images, or personal data.
Update the detailed docs when a decision changes. Commands are in `README.md`.
