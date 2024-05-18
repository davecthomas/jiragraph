import datetime
import os
import time
import json
from typing import Dict, List
import requests
from requests.models import Response
from requests.auth import HTTPBasicAuth
import networkx as nx
from pyvis.network import Network
from dotenv import load_dotenv
import difflib
import re
from networkx.readwrite import json_graph

# Load environment variables
load_dotenv()

JIRA_API_TOKEN = os.getenv('JIRA_API_TOKEN')
JIRA_USER = os.getenv('JIRA_USER')
JIRA_CO_URL = os.getenv('JIRA_CO_URL')
JQL_QUERY = os.getenv('JQL_QUERY')


def sleep_until_ratelimit_reset_time(reset_epoch_time):
    # Convert the reset time from Unix epoch time to a datetime object
    reset_time = datetime.datetime.utcfromtimestamp(reset_epoch_time)

    # Get the current time
    now = datetime.datetime.utcnow()

    # Calculate the time difference
    time_diff = reset_time - now

    # Check if the sleep time is negative, which can happen if the reset time has passed
    if time_diff.total_seconds() < 0:
        print("\tNo sleep required. The rate limit reset time has already passed.")
    else:
        time_diff = datetime.timedelta(seconds=int(time_diff.total_seconds()))
        # Print the sleep time using timedelta's string representation
        print(f"\tSleeping until rate limit reset: {time_diff}")
        time.sleep(time_diff.total_seconds())
    return


def check_API_rate_limit(response: Response) -> bool:
    if response.status_code == 403 and 'X-Ratelimit-Remaining' in response.headers:
        if int(response.headers['X-Ratelimit-Remaining']) == 0:
            print(
                f"\t403 forbidden response header shows X-Ratelimit-Remaining at {response.headers['X-Ratelimit-Remaining']} requests.")
            sleep_until_ratelimit_reset_time(
                int(response.headers['X-RateLimit-Reset']))
    return (response.status_code == 403 and 'X-Ratelimit-Remaining' in response.headers)

# Retry backoff in 422, 202, or 403 (rate limit exceeded) responses


def jira_request_exponential_backoff(url: str, params: Dict = None):
    exponential_backoff_retry_delays_list = [1, 2, 4, 8, 16, 32, 64]
    headers = {
        "Authorization": f"Basic {JIRA_API_TOKEN}",
        "Content-Type": "application/json"
    }

    retry = False
    response = Response()
    retry_url = None

    try:
        response = requests.get(url, auth=HTTPBasicAuth(
            JIRA_USER, JIRA_API_TOKEN), headers=headers, params=params)
    except requests.exceptions.Timeout:
        print("Initial request timed out.")
        retry = True

    if retry or (response is not None and response.status_code != 200):
        if response.status_code == 422 and response.reason == "Unprocessable Entity":
            dict_error = json.loads(response.text)
            print(
                f"Skipping: {response.status_code} {response.reason} for url {url}\n\t{dict_error['message']}\n\t{dict_error['errors'][0]['message']}")

        elif retry or response.status_code == 202 or response.status_code == 403:  # Try again
            for retry_attempt_delay in exponential_backoff_retry_delays_list:
                if 'Location' in response.headers:
                    retry_url = response.headers.get('Location')
                # The only time we override the exponential backoff if we are asked by Jira to wait
                if 'Retry-After' in response.headers:
                    retry_attempt_delay = int(
                        response.headers.get('Retry-After'))
                # Wait for n seconds before checking the status
                time.sleep(retry_attempt_delay)
                retry_response_url = retry_url if retry_url else url
                print(
                    f"Retrying request for {retry_response_url} after {retry_attempt_delay} sec due to {response.status_code} response")
                # A 403 may require us to take a nap
                check_API_rate_limit(response)

                try:
                    response = requests.get(
                        retry_response_url, headers=headers)
                except requests.exceptions.Timeout:
                    print(
                        f"Retry request timed out. retrying in {retry_attempt_delay} seconds.")
                    continue
                # Check if the retry response is 200
                if response.status_code == 200:
                    break  # Exit the loop on successful response
                else:
                    print(
                        f"\tRetried request and still got bad response status code: {response.status_code}")

    if response.status_code == 200:
        return response.json()
    else:
        check_API_rate_limit(response)
        print(
            f"Retries exhausted. Giving up. Status code: {response.status_code}")
        return None


def extract_jira_id(jira_issue_link: str) -> str:
    """
    Extracts the Jira ID from a Jira issue link.

    Args:
        jira_issue_link (str): The Jira issue link.

    Returns:
        str: The extracted Jira ID, or None if the ID cannot be extracted.
    """
    match = re.search(r'/browse/([A-Z0-9-]+)', jira_issue_link)
    return match.group(1) if match else None


def construct_jira_issue_url(issue_id: str) -> str:
    """
    Constructs a Jira issue URL using the environment variable and the given issue ID.

    Args:
        issue_id (str): The Jira issue ID.

    Returns:
        str: The constructed Jira issue URL.
    """
    if not JIRA_CO_URL:
        raise ValueError("JIRA_CO_URL environment variable is not set.")

    return f"https://{JIRA_CO_URL}.atlassian.net/browse/{issue_id}"


def find_closest_field(field_name: str, field_keys):
    matches = difflib.get_close_matches(
        field_name, field_keys, n=1, cutoff=0.6)
    return matches[0] if matches else None

# Get Jira fields and their IDs so we can query them in search API


def get_jira_fields(required_fields: List = ['Epic Link']) -> Dict[str, str]:
    # Jira Cloud API v2, not v3, since v3 does not support /field
    url = f'https://{JIRA_CO_URL}.atlassian.net/rest/api/2/field'
    headers = {
        'Authorization': f'Basic {JIRA_API_TOKEN}',
        'Content-Type': 'application/json'
    }
    response = jira_request_exponential_backoff(url)

    field_mappings = {}

    if response:
        fields = response
        field_keys = {field['name']: field['id'] for field in fields}

        missing_fields = []

        print("Fields found in Jira (keys):")
        for field_name, field_key in field_keys.items():
            print(f" - {field_name}: {field_key}")

        print("\nChecking for required fields:")
        for required_field in required_fields:
            if required_field in field_keys:
                field_mappings[required_field] = field_keys[required_field]
                print(f"Found required field: {
                      required_field} -> {field_keys[required_field]}")
            else:
                closest_match = find_closest_field(
                    required_field, list(field_keys.values()))
                if closest_match:
                    print(f"Required field '{
                          required_field}' not found. Closest match: '{closest_match}'")
                else:
                    print(f"Required field '{
                          required_field}' not found and no close match.")
                missing_fields.append(required_field)

        if missing_fields:
            print("\nMissing or unmatched required fields:")
            for missing_field in missing_fields:
                print(f" - {missing_field}")
        else:
            print("\nAll required fields are present.")

    else:
        print("Failed to fetch fields from Jira.")

    return field_mappings


def fetch_jira_issues(fields: List):
    url = f'https://{JIRA_CO_URL}.atlassian.net/rest/api/3/search'
    headers = {
        'Authorization': f'Basic {JIRA_USER}:{JIRA_API_TOKEN}',
        'Content-Type': 'application/json'
    }
    issues = []
    start_at = 0
    max_results = 50  # Default page size in Jira API

    while True:
        params = {
            'jql': JQL_QUERY,
            'fields': fields,
            'startAt': start_at,
            'maxResults': max_results
        }
        response = jira_request_exponential_backoff(url, params)
        if response:
            issues.extend(response['issues'])
            if len(response['issues']) < max_results:
                break  # No more issues to fetch
            start_at += max_results
        else:
            break  # Exit loop if there was an error or rate limit exceeded

    return issues


def create_graph(field_mappings: Dict[str, str], issues: List):
    G = nx.DiGraph()

    issue_type_field = 'issuetype'
    issue_status_field = 'status'
    issue_parent_field = 'parent'
    issue_epic_field = 'epiclink'
    issue_summary_field = 'summary'

    if "Epic Link" in field_mappings:
        issue_epic_field = field_mappings["Epic Link"]

    for issue in issues:
        issue_id = issue['key']
        issue_type = issue['fields'][issue_type_field]['name']
        issue_status = issue['fields'][issue_status_field]['name']
        summary = issue['fields'][issue_summary_field][:64]

        G.add_node(issue_id, label=summary, type=issue_type,
                   status=issue_status, url=construct_jira_issue_url(issue_id))

        if issue_parent_field in issue['fields']:
            parent_id = issue['fields'][issue_parent_field]['key']
            if parent_id not in G:
                G.add_node(parent_id, label=parent_id, type='Parent',
                           status='Unknown', url=construct_jira_issue_url(parent_id))
            G.add_edge(parent_id, issue_id, type='Parent')
        if issue_epic_field in issue['fields'] and issue['fields'][issue_epic_field] is not None:
            epic_id = issue['fields'][issue_epic_field]
            if epic_id not in G:
                G.add_node(epic_id, label=epic_id, type='Epic',
                           status='Unknown', url=construct_jira_issue_url(epic_id))
            G.add_edge(epic_id, issue_id, type='Epic')

    return G


def node_color(data):
    colors = {
        'Bug': 'red',
        'Task': 'blue',
        'Story': 'green',
        'Epic': 'orange'
    }
    return colors.get(data['type'] if 'type' in data else 'default_type', 'gray')


def node_border_color(status):
    border_colors = {
        'Open': 'black',
        'In Progress': 'yellow',
        'Done': 'green',
        'Closed': 'red'
    }
    return border_colors.get(status, 'gray')


def generate_html_with_graph(field_mappings: Dict[str, str], issues: List, filename='jira_issues_with_graph.html'):
    G = create_graph(field_mappings, issues)
    data = json_graph.node_link_data(G)

    def get_node_status(node_id):
        for node in data['nodes']:
            if node['id'] == node_id:
                return node['status']
        return 'Unknown'

    nodes = json.dumps([{
        'id': node['id'],
        'label': node['label'],
        'title': node['id'],
        'color': {
            'background': node_color(node),
            'border': node_border_color(get_node_status(node['id']))
        },
        'borderWidth': 4,
        'url': node['url']
    } for node in data['nodes']])
    edges = json.dumps([{
        'from': edge['source'],
        'to': edge['target'],
        'label': G.edges[edge['source'], edge['target']]['type'],
        'color': 'blue' if G.edges[edge['source'], edge['target']]['type'] == 'Parent' else 'green',
        'width': 3
    } for edge in data['links']])

    issue_list_items = ''.join(
        [f'<li><a href="https://{JIRA_CO_URL}.atlassian.net/browse/{issue["key"]}" target="_blank">{issue["key"]}: {issue["fields"]["summary"][:64]}</a></li>'
         for issue in issues]
    )

    html_template = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>Jira Issues and Network Graph</title>
        <script src="https://cdnjs.cloudflare.com/ajax/libs/vis/4.21.0/vis.min.js"></script>
        <link href="https://cdnjs.cloudflare.com/ajax/libs/vis/4.21.0/vis.min.css" rel="stylesheet">
        <style>
            body {{
                display: flex;
                font-family: Arial, sans-serif;
            }}
            .left {{
                width: 30%;
                padding: 20px;
                overflow-y: auto; /* Ensure scrolling if content overflows */
                height: 100vh; /* Full height of the viewport */
            }}
            .right {{
                width: 70%;
                padding: 20px;
                position: relative; /* To position the legend absolutely */
            }}
            .legend {{
                position: absolute;
                bottom: 20px;
                right: 20px;
                background: white;
                padding: 10px;
                border: 1px solid lightgray;
                border-radius: 5px;
                box-shadow: 0px 0px 10px rgba(0, 0, 0, 0.1);
            }}
            #mynetwork {{
                width: 100%;
                height: 750px;
                border: 1px solid lightgray;
            }}
        </style>
    </head>
    <body>
        <div class="left">
            <h1>Jira Issues</h1>
            <ul>
                {issue_list_items}
            </ul>
        </div>
        <div class="right">
            <div id="mynetwork"></div>
            <div class="legend">
                <h3>Legend</h3>
                <p><span style="color: red;">●</span> Bug</p>
                <p><span style="color: blue;">●</span> Task</p>
                <p><span style="color: green;">●</span> Story</p>
                <p><span style="color: orange;">●</span> Epic</p>
                <h3>Status Colors</h3>
                <p><span style="color: black;">■</span> Open</p>
                <p><span style="color: yellow;">■</span> In Progress</p>
                <p><span style="color: green;">■</span> Done</p>
                <p><span style="color: red;">■</span> Closed</p>
            </div>
        </div>
        <script>
            var nodes = new vis.DataSet({nodes});
            var edges = new vis.DataSet({edges});

            var container = document.getElementById('mynetwork');
            var data = {{
                nodes: nodes,
                edges: edges
            }};
            var options = {{
                physics: {{
                    enabled: true,
                    solver: 'forceAtlas2Based',
                    forceAtlas2Based: {{
                        gravitationalConstant: -50,
                        centralGravity: 0.01,
                        springLength: 100,
                        springConstant: 0.08,
                        avoidOverlap: 1
                    }},
                    maxVelocity: 50,
                    minVelocity: 0.1,
                    timestep: 0.5,
                    stabilization: {{ iterations: 150 }}
                }},
                layout: {{
                    hierarchical: {{
                        direction: 'UD',  // 'UD' is Up-Down
                        sortMethod: 'directed',
                        nodeSpacing: 200,
                        levelSeparation: 200
                    }}
                }},
                edges: {{
                    font: {{
                        align: 'horizontal'
                    }},
                    smooth: {{
                        type: 'cubicBezier',
                        forceDirection: 'vertical',
                        roundness: 0.4
                    }}
                }},
                nodes: {{
                    shape: 'box',
                    font: {{
                        color: '#ffffff'
                    }},
                    widthConstraint: {{
                        maximum: 200  // Set maximum width for nodes
                    }},
                    chosen: {{
                        node: function(values, id, selected, hovering) {{
                            values.shadowSize = selected ? 10 : 5;
                        }}
                    }}
                }},
                interaction: {{
                    dragNodes: true, // Enable dragging of nodes
                    hover: true, // Enable hovering
                }},
                manipulation: {{
                    enabled: true
                }}
            }};
            var network = new vis.Network(container, data, options);

            network.on("selectNode", function(params) {{
                var node = nodes.get(params.nodes[0]);
                if (node.url) {{
                    window.open(node.url, '_blank');
                }}
            }});
            network.on("hoverNode", function(params) {{
                network.bringToFront(params.node);
            }});
        </script>
    </body>
    </html>
    """

    if os.path.exists(filename):
        base, ext = os.path.splitext(filename)
        counter = 1
        new_filename = f"{base}_{counter}{ext}"
        while os.path.exists(new_filename):
            counter += 1
            new_filename = f"{base}_{counter}{ext}"
        filename = new_filename

    with open(filename, 'w') as f:
        f.write(html_template)


def main():
    # test
    field_mappings: Dict[str, str] = get_jira_fields()
    field_mappings['Parent'] = 'parent'
    field_mappings['Summary'] = 'summary'
    field_mappings['Status'] = 'status'
    field_mappings['Issue Type'] = 'issuetype'
    field_ids: List[str] = list(field_mappings.values())
    issues = fetch_jira_issues(field_ids)
    generate_html_with_graph(field_mappings, issues)


if __name__ == '__main__':
    main()
