# Corrected copy of open-webui's backend/open_webui/retrieval/web/kagi.py
# (pinned to upstream v0.9.6). Upstream ships a broken Kagi Search API call
# that returns 401: it POSTs to /api/v1/search with an "Authorization: Bearer"
# header and a "query" body field. Kagi's live Search API is GET /api/v0/search
# with "Authorization: Bot <token>" and a "q" parameter (verified empirically:
# v1 is a 404, v0 + Bot returns 200). Drop this override once upstream fixes it.
import logging
from typing import Optional

import requests
from open_webui.retrieval.web.main import SearchResult, get_filtered_results

log = logging.getLogger(__name__)


def search_kagi(api_key: str, query: str, count: int, filter_list: Optional[list[str]] = None) -> list[SearchResult]:
    """Search using Kagi's Search API and return the results as a list of SearchResult objects.

    The Search API will inherit the settings in your account, including results personalization and snippet length.

    Args:
        api_key (str): A Kagi Search API key
        query (str): The query to search for
        count (int): The number of results to return
    """
    url = 'https://kagi.com/api/v0/search'
    headers = {
        'Authorization': f'Bot {api_key}',
    }
    params = {'q': query, 'limit': count}

    response = requests.get(url, headers=headers, params=params)
    response.raise_for_status()
    json_response = response.json()
    search_results = json_response.get('data', [])

    results = [
        SearchResult(link=result['url'], title=result['title'], snippet=result.get('snippet'))
        for result in search_results
        if result.get('t') == 0
    ]

    if filter_list:
        results = get_filtered_results(results, filter_list)

    return results
