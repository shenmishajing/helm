import os
import mako.template
from dataclasses import dataclass
from typing import Dict, List, Tuple, Any

from common.general import ensure_directory_exists, parse_hocon
from common.request import Request, RequestResult
from .models import Model
from .query import Query, QueryResult
from .models import all_models, get_model_group
from .accounts import Authentication, Accounts, Account
from .auto_client import AutoClient
from .example_queries import example_queries

VERSION = "1.0"
CREDENTIALS_FILE = "credentials.conf"
ACCOUNTS_FILE = "accounts.jsonl"
CACHE_DIR = "cache"
MAX_EXPANSION = 1000


@dataclass(frozen=True)
class GeneralInfo:
    version: str
    example_queries: List[Query]
    all_models: List[Model]


def expand_environments(environments: Dict[str, List[str]]):
    """
    `environments` is a map from variable names to a list of strings.
    Return: a list of environments, where for each variable, we choose one of its string.
    """
    output_environments: List[Dict[str, str]] = []

    def recurse(old_items: List[Tuple[str, List[str]]], new_items: List[Tuple[str, str]]):
        if len(output_environments) >= MAX_EXPANSION:
            return
        if len(old_items) == 0:
            output_environments.append(dict(new_items))
        else:
            item, rest_old_items = old_items[0], old_items[1:]
            key, list_value = item
            for elem_value in list_value:
                recurse(rest_old_items, new_items + [(key, elem_value)])

    recurse(list(environments.items()), [])
    return output_environments


def substitute_text(text: str, environment: Dict[str, str]) -> str:
    """
    Example:
        text = "Hello {name}"
        environment = {"name": "Sue"}
        Return "Hello Sue"
    """
    return mako.template.Template(text).render(**environment)


def synthesize_request(prompt: str, settings: str, environment: Dict[str, str]) -> Request:
    """Substitute `environment` into `prompt` and `settings`."""
    request: Dict[str, Any] = {}
    request["prompt"] = substitute_text(prompt, environment)
    request.update(parse_hocon(substitute_text(settings, environment)))
    return Request(**request)


class Service(object):
    """
    Main class that supports various functionality for the server.
    """

    def __init__(self, base_path: str = ".", read_only: bool = False):
        credentials_path = os.path.join(base_path, CREDENTIALS_FILE)
        cache_path = os.path.join(base_path, CACHE_DIR)
        ensure_directory_exists(cache_path)
        accounts_path = os.path.join(base_path, ACCOUNTS_FILE)

        if os.path.exists(credentials_path):
            with open(credentials_path) as f:
                credentials = parse_hocon(f.read())
        else:
            credentials = {}
        self.client = AutoClient(credentials, cache_path)
        self.accounts = Accounts(accounts_path, read_only=read_only)

    def finish(self):
        self.accounts.finish()

    def get_general_info(self):
        return GeneralInfo(version=VERSION, example_queries=example_queries, all_models=all_models)

    def expand_query(self, query: Query) -> QueryResult:
        """Turn the `query` into requests."""
        prompt = query.prompt
        settings = query.settings
        environments = parse_hocon(query.environments)
        requests = []
        for environment in expand_environments(environments):
            request = synthesize_request(prompt, settings, environment)
            requests.append(request)
        return QueryResult(requests=requests)

    def make_request(self, auth: Authentication, request: Request) -> RequestResult:
        """Actually make a request to an API."""
        # TODO: try to invoke the API even if we're not authenticated, and if
        # it turns out the results are cached, then we can just hand back the results.

        self.accounts.authenticate(auth)
        model_group = get_model_group(request.model)
        # Make sure we can use
        self.accounts.check_can_use(auth.api_key, model_group)

        # Use!
        request_result = self.client.make_request(request)

        # Only deduct if not cached
        if not request_result.cached:
            # Estimate number of tokens (TODO: fix this)
            count = sum(len(completion.text.split(" ")) for completion in request_result.completions)
            self.accounts.use(auth.api_key, model_group, count)

        return request_result

    def get_account(self, auth: Authentication) -> Account:
        """Get information about an account."""
        self.accounts.authenticate(auth)
        return self.accounts.api_key_to_accounts[auth.api_key]