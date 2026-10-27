# LinkedIn lead kit

This folder finds leads on LinkedIn for one business and sorts them into their Google Sheet. Claude is the judge.

- If `config/profile.json` does not exist, the kit is not set up yet: follow SETUP.md with the user, asking every
  question before deciding anything.
- Otherwise, when asked to run the lead search (or by a scheduled task), follow OPERATOR.md.
- All commands are `python lead.py ...`; `python lead.py help` lists them.
- Never print the Apify token or the Google key, and never contact a lead.
