# aihub-nodes-teams

Node package for the aihub-ui Works editor (guide: docs/ui-nodes.md).

- **Start: Teams message** (`nodes/teams-message.node.json`): listens on 127.0.0.1 for messages relayed by a Teams bot and starts the work for those that match the filter.
- **Send to Teams** (`nodes/teams-send.node.json`): posts the text to a Teams Workflows / incoming webhook URL (stored encrypted).

Both nodes can take their secret from one shared **Teams** credential set (`nodes/teams.cred.json`: tenant, client id, client secret, Workflows URL, listener secret): create it in aihub-ui Settings > Credentials, then pick it in each node. A value typed on a node still wins.
