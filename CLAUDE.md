# SoloPM

## Ticket workflow

Move the ticket through these states as work progresses:

1. **In Progress** — when work on the ticket starts.
2. **In AI Review** — when the work is done. Then run `/gpt-review` on the ticket.
   - If `/gpt-review` **finds issues**: move the ticket back to **In Progress**, fix the issues, then return to step 2 (In AI Review + re-run `/gpt-review`).
   - If `/gpt-review` finds **no issues**: move the ticket to **In Human Review**.

Happy path: In Progress → In AI Review → Resolve Merge Conflicts → In Human Review.
Failed review loops back: In AI Review → In Progress → In AI Review.
