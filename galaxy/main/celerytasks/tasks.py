# (c) 2012-2014, Ansible, Inc. <support@ansible.com>
#
# This file is part of Ansible Galaxy
#
# Ansible Galaxy is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible Galaxy is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

import re
import yaml
import datetime
import requests
import logging
import pytz

from celery import task
from github import Github
from github import GithubException
from urlparse import urlparse

from django.utils import timezone
from django.db import transaction

from allauth.socialaccount.models import SocialToken

from galaxy.main.models import (Platform,
                                Tag,
                                Role,
                                ImportTask,
                                RoleVersion,
                                Namespace)

from galaxy.main.celerytasks.elastic_tasks import update_custom_indexes


logger = logging.getLogger(__name__)

def update_user_repos(github_repos, user):
    '''
    Refresh user repositories. Used by refresh_user_repos task and galaxy.api.views.RefreshUserRepos.
    Returns user.repositories.all() queryset.
    '''
    repo_dict = dict()
    for r in github_repos:
        try:
            r.get_file_contents("meta/main.yml")
            name = r.full_name.split('/')
            cnt = Role.objects.filter(github_user=name[0], github_repo=name[1]).count()
            enabled = cnt > 0
            user.repositories.update_or_create(
                github_user=name[0],
                github_repo=name[1],
                defaults={'github_user': name[0],
                          'github_repo': name[1],
                          'is_enabled': enabled})
            repo_dict[r.full_name] = True
        except:
            pass
        
    # Remove any that are no longer present in GitHub
    for r in user.repositories.all():
        if not repo_dict.get(r.github_user + '/' + r.github_repo):
            r.delete()

    return user.repositories.all()


def update_namespace(repo):
    # Use GitHub repo to update namespace attributes
    if repo.owner.type == 'Organization':
        namespace, created = Namespace.objects.get_or_create(namespace=repo.organization.login, defaults={
                                                             'name': repo.organization.name,
                                                             'avatar_url': repo.organization.avatar_url,
                                                             'location': repo.organization.location,
                                                             'company': repo.organization.company,
                                                             'email': repo.organization.email,
                                                             'html_url': repo.organization.html_url,
                                                             'followers': repo.organization.followers})
        if not created:
            namespace.avatar_url = repo.organization.avatar_url
            namespace.location = repo.organization.location
            namespace.company = repo.organization.company
            namespace.email = repo.organization.email
            namespace.html_url = repo.organization.html_url
            namespace.followers = repo.organization.followers
            namespace.save()
    else:
        namespace, created = Namespace.objects.get_or_create(namespace=repo.owner.login, defaults={
                                                             'name': repo.owner.name,
                                                             'avatar_url': repo.owner.avatar_url,
                                                             'location': repo.owner.location,
                                                             'company': repo.owner.company,
                                                             'email': repo.owner.email,
                                                             'html_url': repo.owner.html_url,
                                                             'followers': repo.owner.followers})
        if not created:
            namespace.avatar_url = repo.owner.avatar_url
            namespace.location = repo.owner.location
            namespace.company = repo.owner.company
            namespace.email = repo.owner.email
            namespace.html_url = repo.owner.html_url
            namespace.followers = repo.owner.followers
            namespace.save()
    return True


def fail_import_task(import_task, msg):
    """
    Abort the import task ans raise an exception
    """
    transaction.rollback()
    try:
        if import_task:
            import_task.state = "FAILED"
            import_task.messages.create(message_type="ERROR", message_text=msg[:255])
            import_task.finished = timezone.now()
            import_task.save()
            transaction.commit()
    except Exception as e:
        transaction.rollback()
        logger.error(u"Error updating import task state %s: %s" % (import_task.role.name, str(e)))
    logger.error(msg)
    raise Exception(msg)


def strip_input(input):
    """
    A simple helper to strip input strings while checking
    to make sure they're not None.
    """
    if input is None:
        return ""
    elif isinstance(input, basestring):
        return input.strip()
    else:
        return input


def add_message(import_task, msg_type, msg_text):
    try:
        import_task.messages.create(message_type=msg_type, message_text=msg_text[:255])
        import_task.save()
        transaction.commit()
    except Exception as e:
        transaction.rollback()
        logger.error(u"Error adding message to import task for role %s: %s" % (import_task.role.name, e.message))
    logger.info(u"Role %d: %s - %s" % (import_task.role.id, msg_type, msg_text))


def get_readme(import_task, repo, branch, token):
    """
    Retrieve README from the repo and sanitize by removing all markup. Should preserve unicode characters.
    """
    file_type = None

    add_message(import_task, "INFO", "Parsing and validating README")

    readme_html = None
    readme_raw = None
    headers = {'Authorization': 'token %s' % token, 'Accept': 'application/vnd.github.VERSION.html'}
    url = "https://api.github.com/repos/%s/readme" % repo.full_name
    data = None
    if import_task.github_reference:
        data = {'ref': branch}
    try:
        response = requests.get(url, headers=headers, data=data)
        response.raise_for_status()
        readme_html = response.text
    except Exception as exc:
        logger.error(u"ERROR Getting readme HTML: %s" % str(exc))
        add_message(import_task, u"ERROR", u"Failed to get HTML version of README: %s" % str(exc))

    try:
        if import_task.github_reference:
            readme = repo.get_readme(ref=branch)
        else:
            readme = repo.get_readme()
    except:
        readme = None
        add_message(import_task, u"ERROR",
                    u"Failed to get README. All role repositories must include a README.")

    if readme is not None:
        if readme.name == 'README.md':
            file_type = 'md'
        elif readme.name == 'README.rst':
            file_type = 'rst'
        else:
            add_message(import_task, u"ERROR", "Unable to determine README file type. Expecting file "
                        u"extension to be one of: .md, .rst")
        readme_raw = readme.decoded_content
    return readme_raw, readme_html, file_type


def decode_yaml_file(import_task, repo, branch, file_name, required=False):
    file_contents = None
    try:
        file_contents = repo.get_file_contents(file_name, ref=branch)
    except Exception as exc:
        if not required:
            pass
        else:
            fail_import_task(import_task, u"Failed to find %s - %s" % (file_name, str(exc)))
    if file_contents:
        try:
            file_contents = file_contents.content.decode('base64')
        except Exception as exc:
            fail_import_task(import_task, u"Failed to decode %s - %s" % (file_name, str(exc)))
        try:
            file_contents = yaml.safe_load(file_contents)
        except yaml.YAMLError as exc:
            add_message(import_task, u"ERROR", u"YAML parse error: %s" % str(exc))
            fail_import_task(import_task, u"Failed to parse %s. Check YAML syntax." % file_name)
    return file_contents


@task(throws=(Exception,), name="galaxy.main.celerytasks.tasks.import_role")
def import_role(task_id):
    try:
        logger.info(u"Starting task: %d" % int(task_id))
        import_task = ImportTask.objects.get(id=task_id)
        import_task.state = "RUNNING"
        import_task.started = timezone.now()
        import_task.save()
        transaction.commit()
    except:
        fail_import_task(None, u"Failed to get task id: %d" % int(task_id))

    try:
        role = Role.objects.get(id=import_task.role.id)
    except:
        fail_import_task(import_task, u"Failed to get role for task id: %d" % int(task_id))

    user = import_task.owner
    repo_full_name = role.github_user + "/" + role.github_repo
    add_message(import_task, u"INFO", u"Starting import %d: role_name=%s repo=%s" % (import_task.id,
                                                                                     role.name,
                                                                                     repo_full_name))
    try:
        token = SocialToken.objects.get(account__user=user, account__provider='github')
    except:
        fail_import_task(import_task, (u"Failed to get Github account for Galaxy user %s. You must first "
                                       u"authenticate with Github." % user.username))

    # create an API object and get the repo
    try:
        gh_api = Github(token.token)
        gh_api.get_api_status()
    except:
        fail_import_task(import_task, (u'Failed to connect to Github API. This is most likely a temporary error, '
                                       u'please retry your import in a few minutes.'))
    
    try:
        gh_user = gh_api.get_user()
    except:
        fail_import_task(import_task, u"Failed to get Github authorized user.")

    repo = None
    add_message(import_task, u"INFO", u"Retrieving Github repo %s" % repo_full_name)
    for r in gh_user.get_repos():
        if r.full_name == repo_full_name:
            repo = r
            continue
    if repo is None:
        fail_import_task(import_task, u"Galaxy user %s does not have access to repo %s" % (user.username,
                                                                                           repo_full_name))

    update_namespace(repo)

    # determine which branch to use
    if import_task.github_reference:
        branch = import_task.github_reference
    elif role.github_branch:
        branch = role.github_branch
    else:
        branch = repo.default_branch
    
    add_message(import_task, u"INFO", u"Accessing branch: %s" % branch)
        
    # parse meta/main.yml data
    add_message(import_task, u"INFO", u"Parsing and validating meta/main.yml")
    meta_data = decode_yaml_file(import_task, repo, branch, 'meta/main.yml', required=True)

    # validate meta/main.yml
    galaxy_info = meta_data.get("galaxy_info", None)
    if galaxy_info is None:
        add_message(import_task, u"ERROR", u"Key galaxy_info not found in meta/main.yml")
        galaxy_info = {}

    if import_task.alternate_role_name:
        add_message(import_task, u"INFO", u"Setting role name to %s" % import_task.alternate_role_name)
        role.name = import_task.alternate_role_name

    role.description         = strip_input(galaxy_info.get("description",repo.description))
    role.author              = strip_input(galaxy_info.get("author",""))
    role.company             = strip_input(galaxy_info.get("company",""))
    role.license             = strip_input(galaxy_info.get("license",""))
    role.min_ansible_version = strip_input(galaxy_info.get("min_ansible_version",""))
    role.issue_tracker_url   = strip_input(galaxy_info.get("issue_tracker_url",""))
    role.github_branch       = strip_input(galaxy_info.get("github_branch", ""))
    role.github_default_branch = repo.default_branch

    # check if meta/container.yml exists
    container_yml = decode_yaml_file(import_task, repo, branch, 'meta/container.yml', required=False)
    ansible_container_yml = decode_yaml_file(import_task, repo, branch, 'ansible/container.yml', required=False)
    if container_yml and ansible_container_yml:
        add_message(import_task, u"ERROR", (u"Found ansible/container.yml and meta/container.yml. "
                                            u"A role can only have only one container.yml file."))
    elif container_yml:
        add_message(import_task, u"INFO", u"Found meta/container.yml")
        add_message(import_task, u"INFO", u"Setting role type to Container")
        role.role_type = Role.CONTAINER
        role.container_yml = container_yml
    elif ansible_container_yml:
        add_message(import_task, u"INFO", u"Found ansible/container.yml")
        add_message(import_task, u"INFO", u"Setting role type to Container App")
        role.role_type = Role.CONTAINER_APP
        role.container_yml = ansible_container_yml

    if role.issue_tracker_url == "" and repo.has_issues:
        role.issue_tracker_url = repo.html_url + '/issues'
    
    if role.company != "" and len(role.company) > 50:
        add_message(import_task, u"WARNING", u"galaxy_info.company exceeds max length of 50 in meta/main.yml")
        role.company = role.company[:50]
    
    if role.description == "":
        add_message(import_task, u"ERROR", u"missing description. Add a description to GitHub repo or meta/main.yml.")
    elif len(role.description) > 255:
        add_message(import_task, u"WARNING", u"galaxy_info.description exceeds max length of 255 in meta/main.yml")
        role.description = role.description[:255]
    
    if role.license == "":
        add_message(import_task, u"ERROR", u"galaxy_info.license missing value in meta/main.yml")
    elif len(role.license) > 50:
        add_message(import_task, u"WARNING", u"galaxy_info.license exceeds max length of 50 in meta/main.yml")
        role.license = role.license[:50]
        
    if role.min_ansible_version == "":
        add_message(import_task, u"WARNING", (u"galaxy.min_ansible_version missing value in meta/main.yml. "
                                              u"Defaulting to 1.9."))
        role.min_ansible_version = u'1.9'
    
    if role.issue_tracker_url == "":
        add_message(import_task, u"WARNING", (u"No issue tracker defined. Enable issue tracker in repo settings "
                                              u"or define galaxy_info.issue_tracker_url in meta/main.yml."))
    else:
        parsed_url = urlparse(role.issue_tracker_url)
        if parsed_url.scheme == '' or parsed_url.netloc == '' or parsed_url.path == '':
            add_message(import_task, u"WARNING", (u"Invalid URL provided for galaxy_info.issue_tracker_url in "
                                                  u"meta/main.yml"))
            role.issue_tracker_url = ""

    # Update role attributes from repo
    sub_count = 0
    for sub in repo.get_subscribers():
        sub_count += 1   # only way to get subscriber count via pygithub
    role.stargazers_count = repo.stargazers_count
    role.watchers_count = sub_count
    role.forks_count = repo.forks_count
    role.open_issues_count = repo.open_issues_count 

    last_commit = repo.get_commits(sha=branch)[0].commit
    role.commit = last_commit.sha
    role.commit_message = last_commit.message[:255]
    role.commit_url = last_commit.html_url
    role.commit_created = last_commit.committer.date.replace(tzinfo=pytz.UTC)

    # Update the import task in the event the role is left in an invalid state.
    import_task.stargazers_count = repo.stargazers_count
    import_task.watchers_count = sub_count
    import_task.forks_count = repo.forks_count
    import_task.open_issues_count = repo.open_issues_count 

    import_task.commit = last_commit.sha
    import_task.commit_message = last_commit.message[:255]
    import_task.commit_url = last_commit.html_url
    import_task.github_branch = branch
    
    # Add tags / categories. Remove ':' and only allow alpha-numeric characters
    add_message(import_task, u"INFO", u"Parsing galaxy_tags")
    meta_tags = []
    if galaxy_info.get("categories", None):
        add_message(import_task, u"WARNING", (u"Found galaxy_info.categories. Update meta/main.yml to use "
                                              u"galaxy_info.galaxy_tags."))
        cats = galaxy_info.get("categories")
        if isinstance(cats,basestring) or not hasattr(cats, '__iter__'):
            add_message(import_task, u"ERROR", u"In meta/main.yml galaxy_info.categories must be an iterable list.")
        else:
            for category in cats:
                for cat in category.split(':'): 
                    if re.match('^[a-zA-Z0-9]+$',cat):
                        meta_tags.append(cat)
                    else:
                        add_message(import_task, u"WARNING", u"%s is not a valid tag" % cat)
    
    if galaxy_info.get("galaxy_tags", None):
        tags = galaxy_info.get("galaxy_tags")
        if isinstance(tags,basestring) or not hasattr(tags, '__iter__'):
            add_message(import_task, u"ERROR", u"In meta/main.yml galaxy_info.galaxy_tags must be an iterable list.")
        else:
            for tag in tags:
                for t in tag.split(':'):
                    if re.match('^[a-zA-Z0-9:]+$',t):
                        meta_tags.append(t)
                    else:
                        add_message(import_task, u"WARNING", u"'%s' is not a valid tag. Skipping." % t)

    # There are no tags?
    if len(meta_tags) == 0:
        add_message(
            import_task, u"ERROR", (u"No values found for galaxy_tags. galaxy_info.galaxy_tags must be an iterable "
                                    u"list in meta/main.yml"))

    if len(meta_tags) > 20:
        add_message(
            import_task, "WARNING", (u"Found more than 20 values for galaxy_info.galaxy_tags in meta/main.yml. "
                                     u"Only the first 20 will be used."))
        meta_tags = meta_tags[0:20]

    meta_tags = list(set(meta_tags))

    for tag in meta_tags:
        pg_tags = Tag.objects.filter(name=tag).all()
        if len(pg_tags) == 0:
            pg_tag = Tag(name=tag, description=tag, active=True)
            pg_tag.save()
        else:
            pg_tag = pg_tags[0]
        if not role.tags.filter(name=tag).exists():
            role.tags.add(pg_tag)

    # Remove tags no longer listed in the metadata
    for tag in role.tags.all():
        if tag.name not in meta_tags:
            role.tags.remove(tag)

    # Add in the platforms and versions
    add_message(import_task, u"INFO", u"Parsing platforms")
    meta_platforms = galaxy_info.get("platforms", None)
    if meta_platforms is None:
        add_message(import_task, u"ERROR",
                    u"galaxy_info.platforms not defined in meta.main.yml. Must be an iterable list.")
    elif isinstance(meta_platforms,basestring) or not hasattr(meta_platforms, '__iter__'):
        add_message(import_task, u"ERROR",
                    u"Failed to iterate platforms. galaxy_info.platforms must be an iterable list in meta/main.yml.")
    else:
        platform_list = []
        for platform in meta_platforms:
            if not isinstance(platform, dict):
                add_message(import_task, u"ERROR",
                            u"The platform '%s' does not appear to be a dictionary, skipping" % str(platform))
                continue
            try:
                name     = platform.get("name")
                versions = platform.get("versions", ["all"])
                if not name:
                    add_message(import_task, u"ERROR", u"No name specified for platform, skipping")
                    continue
                elif not isinstance(versions, list):
                    add_message(import_task, u"ERROR", u"No name specified for platform %s, skipping" % name)
                    continue

                if len(versions) == 1 and versions == ["all"] or 'all' in versions:
                    # grab all of the objects that start
                    # with the platform name
                    try:
                        platform_objs = Platform.objects.filter(name=name)
                        for p in platform_objs:
                            role.platforms.add(p)
                            platform_list.append("%s-%s" % (name, p.release))
                    except:
                        add_message(import_task, u"ERROR", u"Invalid platform: %s-all (skipping)" % name)
                else:
                    # just loop through the versions and add them
                    for version in versions:
                        try:
                            p = Platform.objects.get(name=name, release=version)
                            role.platforms.add(p)
                            platform_list.append(u"%s-%s" % (name, p.release))
                        except:
                            add_message(import_task, u"ERROR", u"Invalid platform: %s-%s (skipping)" % (name,version))
            except Exception, e:
                add_message(import_task, u"ERROR", u"An unknown error occurred while adding platform: %s" % str(e))
            
        # Remove platforms/versions that are no longer listed in the metadata
        for platform in role.platforms.all():
            platform_key = "%s-%s" % (platform.name, platform.release)
            if platform_key not in platform_list:
                role.platforms.remove(platform)

    # Add in dependencies (if there are any):
    add_message(import_task, u"INFO", u"Adding dependencies")
    dependencies = meta_data.get("dependencies", None)

    if dependencies is None:
        add_message(import_task, u"ERROR",
                    u"meta/main.yml missing definition for dependencies. Define dependencies as [] or an iterable list.")
    elif isinstance(dependencies,basestring) or not hasattr(dependencies, '__iter__'):
        add_message(import_task, u"ERROR",
                    u"Failed to iterate dependencies. Define dependencies in meta/main.yml as [] or an iterable list.")
    else:
        dep_names = []
        for dep in dependencies:
            names = []
            try:
                if isinstance(dep, dict):
                    if 'role' not in dep:
                        raise Exception(u"'role' keyword was not found in the role dictionary")
                    else:
                        dep = dep['role']
                elif not isinstance(dep, basestring):
                    raise Exception(u"role dependencies must either be a string or dictionary (was %s)" % type(dep))

                names = dep.split(".")
                if len(names) < 2:
                    raise Exception(u"name format must match 'username.name'")
            except Exception, e:
                add_message(import_task, u"ERROR",
                            u"Invalid role dependency: %s (skipping) (error: %s)" % (str(dep),e))
            if len(names) > 2:
                # the username contains .
                name = names[len(names) - 1]
                names.pop()
                namespace = '.'.join(names)
                try:
                    dep_role = Role.objects.get(namespace=namespace, name=name)
                    role.dependencies.add(dep_role)
                    dep_names.append(dep)
                except:
                    add_message(import_task, u"ERROR",
                                u"Role dependency not found name: %s namespace: %s" % (name, namespace))
            else:
                # standard foramt namespace.role_name
                try:
                    dep_role = Role.objects.get(namespace=names[0], name=names[1])
                    role.dependencies.add(dep_role)
                    dep_names.append(dep)
                except:
                    add_message(import_task, u"ERROR", u"Role dependency not found: %s" % dep)

        # Remove deps that are no longer listed in the metadata
        for dep in role.dependencies.all():
            dep_name = dep.__unicode__()
            if dep_name not in dep_names:
                role.dependencies.remove(dep)

    role.readme, role.readme_html, role.readme_type = get_readme(import_task, repo, branch, token)
    
    # helper function to save repeating code:
    def add_role_version(tag):
        rv,created = RoleVersion.objects.get_or_create(name=tag.name, role=role)
        rv.release_date = tag.commit.commit.author.date
        rv.save()

    # get the tags for the repo, and then loop through them
    # so we can create version objects for the role
    add_message(import_task, u"INFO", u"Adding repo tags as role versions")
    try:
        git_tag_list = repo.get_tags()
        for git_tag in git_tag_list:
            add_role_version(git_tag)
    except Exception, e:
        add_message(import_task, u"ERROR", u"An error occurred while importing repo tags: %s" % str(e))
    
    try:
        role.validate_char_lengths()
    except Exception, e:
        add_message(import_task, u"ERROR", e.message)

    try:
        import_task.validate_char_lengths()
    except Exception, e:
        add_message(import_task, u"ERROR", e.message)

    # determine state of import task
    error_count = import_task.messages.filter(message_type="ERROR").count()
    warning_count = import_task.messages.filter(message_type="WARNING").count()
    import_state = u"SUCCESS" if error_count == 0 else u"FAILED"
    add_message(import_task, u"INFO", u"Import completed")
    add_message(import_task, import_state, u"Status %s : warnings=%d errors=%d" % (import_state,
                                                                                   warning_count,
                                                                                   error_count))
    
    try:
        import_task.state = import_state
        import_task.finished = timezone.now()
        import_task.save()
        role.imported = timezone.now()
        role.is_valid = True
        role.save()
        transaction.commit()
    except Exception, e:
        fail_import_task(import_task, u"Error saving role: %s" % e.message)

    # Update ES indexes
    update_custom_indexes.delay(username=role.namespace,
                                tags=role.get_tags(),
                                platforms=role.get_unique_platforms())
    return True

# ----------------------------------------------------------------------
# Login Task
# ----------------------------------------------------------------------


@task(name="galaxy.main.celerytasks.tasks.refresh_user", throws=(Exception,))
@transaction.atomic
def refresh_user_repos(user, token):
    logger.info(u"Refreshing User Repo Cache for {}".format(user.username).encode('utf-8').strip())
    
    try:
        gh_api = Github(token)
    except GithubException as exc:
        user.cache_refreshed = True
        user.save()
        msg = u"User {0} Repo Cache Refresh Error: {1}".format(user.username, str(exc).encode('utf-8').strip())
        logger.error(msg)
        raise Exception(msg)

    try:
        ghu = gh_api.get_user()
    except GithubException as exc:
        user.cache_refreshed = True
        user.save()
        msg = u"User {0} Repo Cache Refresh Error: {1}".format(user.username, str(exc)).encode('utf-8').strip()
        logger.error(msg)
        raise Exception(msg)

    try:
        repos = ghu.get_repos()
    except GithubException as exc:
        user.cache_refreshed = True
        user.save()
        msg = u"User {0} Repo Cache Refresh Error: {1}".foramt(user.username, str(exc)).encode('utf-8').strip()
        logger.error(msg)
        raise Exception(msg)

    update_user_repos(repos, user)
    
    user.github_avatar = ghu.avatar_url
    user.github_user = ghu.login
    user.cache_refreshed = True
    user.save()


@task(name="galaxy.main.celerytasks.tasks.refresh_user_stars", throws=(Exception,))
@transaction.atomic
def refresh_user_stars(user, token):
    logger.info(u"Refreshing User Stars for {}".format(user.username).encode('utf-8').strip())

    try:
        gh_api = Github(token)
    except GithubException as exc:
        msg = u"User {0} Refresh Stars: {1}".format(user.username, str(exc)).encode('utf-8').strip()
        logger.error(msg)
        raise Exception(msg)

    try:
        ghu = gh_api.get_user()
    except GithubException as exc:
        msg = u"User {0} Refresh Stars: {1}" % (user.username, str(exc)).encode('utf-8').strip()
        logger.error(msg)
        raise Exception(msg)

    try:
        subscriptions = ghu.get_subscriptions()
    except GithubException as exc:
        msg = u"User {0} Refresh Stars: {1]" % (user.username, str(exc)).encode('utf-8').strip()
        logger.error(msg)
        raise Exception(msg)

    # Refresh user subscriptions class
    user.subscriptions.all().delete()
    for s in subscriptions:
        name = s.full_name.split('/')
        cnt = Role.objects.filter(github_user=name[0], github_repo=name[1]).count()
        if cnt > 0:
            user.subscriptions.get_or_create(
                github_user=name[0],
                github_repo=name[1],
                defaults={
                    'github_user': name[0],
                    'github_repo': name[1]
                })
    
    try:
        starred = ghu.get_starred()
    except GithubException as exc:
        msg = u"User {0} Refresh Stars: {1}".format(user.username, str(exc)).encode('utf-8').strip()
        logger.error(msg)
        raise Exception(msg)

    # Refresh user starred cache
    user.starred.all().delete()
    for s in starred:
        name = s.full_name.split('/')
        cnt = Role.objects.filter(github_user=name[0], github_repo=name[1]).count()
        if cnt > 0:
            user.starred.get_or_create(
                github_user=name[0],
                github_repo=name[1],
                defaults={
                    'github_user': name[0],
                    'github_repo': name[1]    
                })


@task(name="galaxy.main.celerytasks.tasks.refresh_role_counts")
def refresh_role_counts(start, end, gh_api, tracker):
    '''
    Update each role with latest counts from GitHub
    '''
    tracker.state = 'RUNNING'
    tracker.save()
    passed = 0
    failed = 0
    for role in Role.objects.filter(is_valid=True, active=True, id__gt=start, id__lte=end):
        full_name = "%s/%s" % (role.github_user,role.github_repo)
        logger.info(u"Updating repo: {0}".format(full_name).encode('utf-8').strip())
        try:
            gh_repo = gh_api.get_repo(full_name)
            update_namespace(gh_repo)
            # sub_count = len(gh_repo.get_subscribers())
            role.watchers_count = gh_repo.watchers
            role.stargazers_count = gh_repo.stargazers_count
            role.forks_count = gh_repo.forks_count
            role.open_issues_count = gh_repo.open_issues_count
            role.save()
            passed += 1
        except Exception as exc:
            logger.error(u"FAILED %s: %s" % (full_name, str(exc)).encode('utf-8').strip())
            failed += 1
    tracker.state = 'FINISHED'
    tracker.passed = passed
    tracker.failed = failed
    tracker.save()


# ----------------------------------------------------------------------
# Periodic Tasks
# ----------------------------------------------------------------------

@task(name="galaxy.main.celerytasks.tasks.clear_stuck_imports")
def clear_stuck_imports():
    two_hours_ago = timezone.now() - datetime.timedelta(seconds=7200)
    logger.info(u"Clear Stuck Imports: {}".format(two_hours_ago.strftime("%m-%d-%Y %H:%M:%S")).encode('utf-8').strip())
    try:
        for ri in ImportTask.objects.filter(created__lte=two_hours_ago, state='PENDING'):
            logger.info(u"Clear Stuck Imports: {0} - {1}.{2}"
                        .format(ri.id, ri.role.namespace, ri.role.name)
                        .encode('utf-8').strip())
            ri.state = u"FAILED"
            ri.messages.create(
                message_type=u"ERROR",
                message_text=(u"Import timed out, please try again. If you continue seeing this message you may "
                              u"have a syntax error in your meta/main.yml file.")
            )
            ri.save()
            transaction.commit()
    except Exception as exc:
        logger.error(u"Clear Stuck Imports ERROR: {}".format(str(exc)).encode('utf-8').strip())
        raise
