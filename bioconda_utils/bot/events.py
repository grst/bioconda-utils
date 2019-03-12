"""
Github Events
"""

import logging
import re

import gidgethub.routing

from .commands import command_routes
from .tasks import lint_check, PRInfo
from .config import APP_ID
from ..githubhandler import CheckRunStatus, CheckRunConclusion

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name
event_routes = gidgethub.routing.Router()  # pylint: disable=invalid-name

BOT_ALIAS_RE = re.compile(r'@bioconda[- ]?bot', re.IGNORECASE)


@event_routes.register("issue_comment", action="created")
async def comment_created(event, ghapi, *args, **_kwargs):
    """Dispatches @bioconda-bot commands

    This function watches for comments on issues. Lines starting with
    an @mention of the bot are considered commands and dispatched.
    """
    commands = [
        line.lower().split()[1:]
        for line in event.data['comment']['body'].splitlines()
        if BOT_ALIAS_RE.match(line)
    ]
    if not commands:
        logger.info("No command in comment")
    for cmd, *args in commands:
        logger.info("Dispatching %s - %s", cmd, args)
        await command_routes.dispatch(cmd, event, ghapi, *args)


@event_routes.register("status")
async def demo_using_status(_event, _ghapi):
    """Test function for status events"""
    logger.warning("Status - not handling")


async def create_check_run(event, ghapi):
    """Create a new check run"""
    try:
        head_sha = event.get("check_suite/head_sha")
    except KeyError:
        head_sha = event.get("check_run/head_sha")
    check_run_number = await ghapi.create_check_run("Linting Recipe(s)", head_sha)
    logger.warning("Created check run %s", check_run_number)


async def initiate_check_run(event, ghapi):
    check_run_number = event.get('check_run/id')
    head_sha = event.get('check_run/head_sha')
    prs = event.get('check_run/check_suite/pull_requests')

    if not prs:
        ghapi.modify_check_run(check_run_number,
                               status=CheckRunStatus.completed,
                               conclusion=CheckRunConclusion.neutral,
                               output_title="No PRs associated",
                               outout_summary="Merges commits are not linted")
        return

    issue_number = prs[0]['number']
    pr = await ghapi.get_prs(number=int(issue_number))
    if not pr:
        ghapi.modify_check_run(check_run_number,
                               status=CheckRunStatus.completed,
                               conclusion=CheckRunConclusion.neutral,
                               output_title="PR not found",
                               outout_summary="PR {} not found?".format(issue_number))
        logger.error("No PRs with number %s?", pr['number'])
        return

    files = await ghapi.get_pr_modified_files(number=issue_number)
    recipes = [item['filename'] for item in files
               if item['filename'].endswith('/meta.yaml')]
    if not recipes:
        ghapi.modify_check_run(check_run_number,
                               status=CheckRunStatus.completed,
                               conclusion=CheckRunConclusion.success,
                               output_title="No recipes modified by PR",
                               output_summary="No need to check anything.")
        return

    user = pr['head']['user']['login']
    repo = pr['head']['repo']['name']
    ref = pr['head']['ref']
    pr_info = PRInfo(event.get('installation/id'), user, repo, ref, recipes, issue_number)

    lint_check.schedule(pr_info, head_sha, check_run_number, ghapi=ghapi)


@event_routes.register("check_suite")
async def handle_check_suite(event, ghapi):
    """Handle check suite event
    """
    action = event.get('action')
    if action not in ['requested', 'rerequested']:
        return

    prs = event.get("check_suite/pull_requests", [])
    if not prs:
        logger.error("Check_suite event had no associated pull requests (merge?)")
        return

    await create_check_run(event, ghapi)

@event_routes.register("check_run")
async def handle_check_run(event, ghapi):
    """Handle check run event"""
    # Ignore check runs coming from other apps
    if event.get("check_run/app/id") != int(APP_ID):
        return
    action = event.get('action')
    if action == "rerequested":
        await create_check_run(event, ghapi)
    elif action == "created":
        await initiate_check_run(event, ghapi)
