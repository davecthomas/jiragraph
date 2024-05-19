# jiragraph

List and graph issues from a Jira JQL query

# Install

python3 -m venv venv
source venv/bin/activate
pip3 install -r requirements.txt

# Settings

.env file should have

```
JIRA_API_TOKEN=
JIRA_USER=you@you.com
JIRA_CO_URL=your_org_name
JIRA_QUERY="project=YOU and status = Open"
```

# Run it

```
python3 jiragraph.py
```

Outputs an html file that shows you what you need in the browser.
