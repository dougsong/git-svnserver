
from email.utils import parseaddr
import os
import re
import time
import uuid

from GitSvnServer import repos
from GitSvnServer.errors import *

from cat_file import *
from data import *
from db import *


class GitFile (object):
    def __init__(self, commit, path):
        self.commit = commit
        self.path = path
        cmd = '--bare hash-object -w --stdin'
        self.hash_object = GitData(commit.repos.config.location, cmd)

    def write(self, data):
        self.hash_object.write(data)

    def close(self):
        self.hash_object.close_stdin()
        sha1 = self.hash_object.read().strip()
        self.hash_object.close()
        self.commit.file_complete(self.path, sha1)


class GitCommit (object):
    def __init__(self, repos, ref, parent, prefix):
        self.repos = repos
        self.ref = ref
        self.parent = parent
        self.prefix = prefix
        self.files = {}

    def file_complete(self, path, sha1):
        self.files.setdefault(path, {})['sha1'] = sha1

    def modify_file(self, path, rev=None):
        if rev is not None:
            sha1 = self.repos.map.find_commit(self.ref, rev)
            if self.repos._path_changed(sha1, self.parent, path):
                raise PathChanged(path)
        if len(self.prefix) > 0:
            path = '/'.join((self.prefix, path))
        return GitFile(self, path)

    def set_file_prop(self, path, name, value):
        self.files.setdefault(path, {}).setdefault('props', {})[name] = value


class Git (repos.Repos):
    _kind = 'git'

    def __init__(self, host, base, config):
        super(Git, self).__init__(host, base, config)
        self.map = GitMap(self, config.location)
        self.auth_db = GitAuth(self, self.config.location)
        self.cat_file = GitCatFile(config.location)
        self.trunk_re = re.compile(r'^%s/%s(/(?P<path>.*))?$' %
                                   (self.base_url, config.trunk))
        branches = config.branches.replace('$(branch)', '(?P<branch>[^/]+)')
        self.branch_re = re.compile(r'^%s/%s(/(?P<path>.*))?$' %
                                    (self.base_url, branches))
        tags = config.tags.replace('$(tag)', '(?P<tag>[^/]+)')
        self.tag_re = re.compile(r'^%s/%s(/(?P<path>.*))?$' %
                                 (self.base_url, tags))
        self.other_re = re.compile(r'^%s(/(?P<path>.*))?$' %
                                   (self.base_url))

    def __get_git_data(self, command_string):
        git_data = GitData(self.config.location, command_string)

        git_data.open()

        data = [line.strip('\n') for line in git_data._data]

        git_data.close()

        return data

    def __map_url(self, url):
        if url == self.base_url:
            return (None, '')

        trunk_m = self.trunk_re.search(url)
        branch_m = self.branch_re.search(url)
        tag_m = self.tag_re.search(url)
        other_m = self.other_re.search(url)

        if trunk_m:
            path = trunk_m.group('path')
            ref = 'refs/heads/master'
        elif branch_m:
            branch = branch_m.group('branch')
            path = branch_m.group('path')
            ref = 'refs/heads/%s' % branch
        elif tag_m:
            tag = tag_m.group('tag')
            path = tag_m.group('path')
            ref = 'refs/tags/%s' % tag
        elif other_m:
            path = other_m.group('path')
            ref = None
        else:
            raise foo

        if path == None:
            path = ''

        return (ref, path)

    def __map_type(self, type):
        if type is None:
            return 'none'

        if type == 'blob':
            return 'file'

        if type == 'tree':
            return 'dir'

    def _calc_uuid(self):
        self.uuid = self.map.get_uuid()

    def get_latest_rev(self):
        return self.map.get_latest_rev()

    def __get_object(self, sha1):
        return self.cat_file.get_object(sha1)

    def __ls_tree(self, sha1, path, options=''):
        results = []

        cmd = 'ls-tree -l %s %s "%s"' % (options, sha1, path)

        for line in self.__get_git_data(cmd):
            stat, git_path = line.split('\t', 1)
            mode, type, sha, size = stat.split()
            if size == '-':
                size = 0
            results.append((mode, type, sha, int(size), git_path))

        return results

    def __log(self, sha1, path, pretty=None, format=None, options=''):
        if pretty is not None:
            options = '%s "--pretty=%s"' % (options, pretty)

        elif format is not None:
            options = '%s "--pretty=format:%s"' % (options, format)

        cmd = 'log %s %s -- %s' % (options, sha1, path)

        return self.__get_git_data(cmd)

    def __rev_list(self, sha1, path, count=None):
        results = []

        c = ""
        if count is not None:
            c = "-n %d" % count
        cmd = 'rev-list %s %s -- %s' % (c, sha1, path)

        for line in self.__get_git_data(cmd):
            results.append(line.strip())

        return results

    def __commit_info(self, sha1):
        parents = []

        obj = self.__get_object(sha1)
        data = obj.read()
        obj.close()

        c = 0
        for line in data.split('\n'):
            c += 1
            if line == '':
                break
            elif line.startswith('tree'):
                tree = line[4:].strip()
            elif line.startswith('parent'):
                parents.append(line[6:].strip())
            elif line.startswith('author'):
                author = line[6:-16].strip()
                name, email = parseaddr(author)
                when = int(line[-16:-6].strip())
                tz = int(line[-5:].strip())

        msg = '\n'.join(data[c:])

        tz_secs = 60 * (60 * (tz/100) + (tz%100))

        date = time.strftime('%Y-%m-%dT%H:%M:%S.000000Z',
                             time.gmtime(when + tz_secs))

        return tree, parents, name, email, date, msg

    def __get_file_contents(self, mode, sha):
        contents = self.__get_object(sha)

        if mode == '120000':
            link = 'link %s' % contents.read()
            contents.close()
            contents = FakeData(link)

        return contents

    def __get_last_changed(self, sha1, path):
        changed_shas = self.__rev_list(sha1, path, count=1)

        if len(changed_shas) != 1:
            raise foo
        else:
            last_commit = changed_shas[0]
            ref, changed = self.map.get_ref_rev(last_commit)
            t, p, n, by, at, m = self.__commit_info(last_commit)

        return changed, by, at

    def __get_changed_paths(self, sha1, path=''):
        cmd = 'diff-tree --name-status -r %s^ %s -- %s' % (sha1, sha1, path)

        changed_files = {}
        for line in self.__get_git_data(cmd):
            change, path = line.split('\t', 1)
            changed_files[path] = change

        return changed_files

    def __translate_special(self, sha1):
        mode, sha = '', None

        obj = self.__get_object(sha1)
        data = obj.read()
        obj.close()

        if data.startswith('link '):
            mode = '120000'
            cmd = '--bare hash-object -w --stdin'
            ho = GitData(self.config.location, cmd)
            ho.write(data[5:])
            ho.close_stdin()
            sha = ho.read().strip()
            ho.close()

        return mode, sha

    def __get_svn_props(self, mode):
        props = []

        if mode == '120000':
            props.append(('svn:special', '*'))
        elif mode == '100755':
            props.append(('svn:executable', '*'))

        return props

    def __svn_internal_props(self, changed, by, at):
        props = []

        props.append(('svn:entry:uuid', self.get_uuid()))
        props.append(('svn:entry:committed-rev', str(changed)))
        props.append(('svn:entry:committed-date', at))
        props.append(('svn:entry:last-author', by))

        return props

    def _path_changed(self, sha1, sha2, path):
        cmd = 'diff-tree --name-only %s %s -- %s' % (sha1, sha2, path)
        changes = self.__get_git_data(cmd)

        return len(changes) != 0

    def check_path(self, url, rev):
        ref, path = self.__map_url(url)
        sha1 = self.map.find_commit(ref, rev)

        if sha1 is None:
            return 'none'

        if path == '':
            return 'dir'

        data = self.__ls_tree(sha1, path)

        if len(data) != 1:
            return 'none'

        return self.__map_type(data[0][1])

    def stat(self, url, rev):
        ref, path = self.__map_url(url)
        sha1 = self.map.find_commit(ref, rev)

        if ref is None and path == '':
            # We have been asked about the respository root, we don't have one
            # so we have to make something up.
            sha1 = self.map.get_commit_by_rev(rev)
            if sha1 is None:
                changed, by, at = rev, None, self.map.created_date()
            else:
                changed, by, at = self.__get_last_changed(sha1, path)
            return '', 'dir', 0, changed, by, at

        if sha1 is None:
            return None, None, 0, 0, None, None

        if path == '':
            type = 'tree'
            size = 0

        else:
            data = self.__ls_tree(sha1, path)

            if len(data) != 1:
                return None, None, 0, 0, None, None

            mode, type, sha, size, git_path = data[0]

        kind = self.__map_type(type)

        changed, by, at = self.__get_last_changed(sha1, path)

        return path, kind, size, changed, by, at

    def ls(self, url, rev, include_changed=True):
        ref, path = self.__map_url(url)
        sha1 = self.map.find_commit(ref, rev)

        ls_data = []

        if len(path) > 0 and path[-1] != '/':
            path += '/'

        for mode, type, sha, size, name in self.__ls_tree(sha1, path):
            kind = self.__map_type(type)
            if include_changed :
                changed, by, at = self.__get_last_changed(sha1, name)
            else:
                changed, by, at = None, None, None
            if name.startswith(path):
                name = name[len(path):]
            if name == '.gitignore':
                continue
            ls_data.append((name, kind, size, changed, by, at))

        return ls_data

    def log(self, url, target_paths, start_rev, end_rev, limit):
        ref, path = self.__map_url(url)

        if start_rev > end_rev:
            frm = end_rev
            to = start_rev
            order = 'DESC'
        else:
            frm = start_rev
            to = end_rev
            order = 'ASC'

        commits = self.map.get_commits(ref, frm, to, order)

        print path, target_paths

        for row in commits:
            #rev, action, sha1, origin = row
            rev = row['revision']
            sha1 = row['sha1']

            changed = []
            for p, c in self.__get_changed_paths(sha1, path).items():
                for tp in target_paths:
                    if p.startswith(tp):
                        changed.append((p, c, None, None))

            if len(changed) == 0:
                continue

            has_children = False
            revprops = []
            t, p, n, author, date, msg = self.__commit_info(sha1)

            yield (changed, rev, author, date, msg, has_children, revprops)

    def rev_proplist(self, rev):
        raise repos.UnImplemented

    def get_props(self, url, rev, include_internal=True, mode=None):
        ref, path = self.__map_url(url)
        sha1 = self.map.find_commit(ref, rev)

        props = []

        if path != '':
            if mode is not None:
                data = [[mode]]
            else:
                data = self.__ls_tree(sha1, path)

            if len(data) == 1:
                mode = data[0][0]

                props.extend(self.__get_svn_props(mode))

        if not include_internal:
            return props

        changed, by, at = self.__get_last_changed(sha1, path)

        props.extend(self.__svn_internal_props(changed, by, at))

        return props

    def path_changed(self, url, rev, prev_rev):
        ref, path = self.__map_url(url)
        old_sha = self.map.find_commit(ref, rev)
        new_sha = self.map.find_commit(ref, prev_rev)

        return self._path_changed(old_sha, new_sha, path)

    def get_file(self, url, rev):
        ref, path = self.__map_url(url)
        sha1 = self.map.find_commit(ref, rev)

        if path == '':
            return rev, [], None

        data = self.__ls_tree(sha1, path)

        if len(data) != 1:
            return rev, [], None

        mode, type, sha, size, git_path = data[0]

        if type != 'blob':
            return rev, [], None

        props = self.get_props(url, rev, mode=mode)

        return rev, props, self.__get_file_contents(mode, sha)

    def get_files(self, url, rev):
        ref, path = self.__map_url(url)
        sha1 = self.map.find_commit(ref, rev)

        # find out what files exist in the requested commit under the given path

        paths = {}
        for mode, type, sha, size, name in self.__ls_tree(sha1, path,
                                                          options='-r -t'):
            paths[name] = (type, mode, sha)

        # find out when the files discovered above last changed, and who made
        # that commit

        commit, email, data = None, None, None
        changed_paths = {}
        changed_cache = {}
        for line in self.__log(sha1, path, format='/c/%H%n/a/%ae%n/d/%ad%n//',
                               options='--date=raw --name-only'):
            if line.startswith('/c/'):
                commit = line[3:]
                continue

            if line.startswith('/a/'):
                email = line[3:]
                continue

            if line.startswith('/d/'):
                when = int(line[3:-6].strip())
                tz = int(line[-5:].strip())
                tz_secs = 60 * (60 * (tz/100) + (tz%100))
                date = time.strftime('%Y-%m-%dT%H:%M:%S.000000Z',
                                     time.gmtime(when + tz_secs))
                continue

            if line.startswith('//'):
                ref, changed = self.map.get_ref_rev(commit)
                changed_cache[commit] = changed, email, date
                continue

            if line in paths and line not in changed_paths:
                changed_paths[line] = commit

            if len(changed_paths) == len(paths):
                break

        # assemble the recursive structure for the update command

        file_data = {}

        for fpath, (type, mode, sha) in paths.items():
            if '/' in fpath:
                parent, name = fpath.rsplit('/', 1)
            else:
                parent, name = '', fpath

            if fpath not in changed_paths:
                last_commit = sha1
            else:
                last_commit = changed_paths[fpath]
            if last_commit in changed_cache:
                changed, by, at = changed_cache[last_commit]
            else:
                print "changed_cache miss"
                ref, changed = self.map.get_ref_rev(last_commit)
                t, p, n, by, at, m = self.__commit_info(last_commit)
                changed_cache[last_commit] = changed, by, at

            props = self.__svn_internal_props(changed, by, at)
            contents = []

            parent_name = parent
            if '/' in parent:
                parent_name = parent.rsplit('/', 1)[-1]
            def_parent = [parent_name, 'dir', [], []]

            if name == '.gitignore':
                contents = self.__get_file_contents(mode, sha)
                prop = 'svn:ignore', contents.read()
                contents.close()
                file_data.setdefault(parent, def_parent)[2].append(prop)
                continue

            if type == 'blob':
                contents = self.__get_file_contents(mode, sha)
                props.extend(self.__get_svn_props(mode))

            if fpath in file_data:
                file_data[fpath][2].extend(props)
            else:
                kind = self.__map_type(type)
                file_data[fpath] = [name, kind, props, contents]

            file_data.setdefault(parent, def_parent)[3].append(file_data[fpath])

        return file_data[path]

    def start_commit(self, url):
        ref, path = self.__map_url(url)

        cmd = '--bare rev-parse %s' % ref
        parent = self.__get_git_data(cmd)[0]

        return GitCommit(self, ref, parent, path)

    def complete_commit(self, commit, msg):
        orig_index_file = os.environ.get('GIT_INDEX_FILE', '')
        orig_author = os.environ.get('GIT_AUTHOR_NAME', ''), \
                      os.environ.get('GIT_AUTHOR_EMAIL', '')
        orig_committer = os.environ.get('GIT_COMMITTER_NAME', ''), \
                         os.environ.get('GIT_COMMITTER_EMAIL', '')

        os.environ['GIT_INDEX_FILE'] = 'svnserver/tmp-index'

        if self.username is not None:
            name, email = self.auth_db.get_user_details(self.username)

            os.environ['GIT_AUTHOR_NAME'] = name
            os.environ['GIT_AUTHOR_EMAIL'] = email
            os.environ['GIT_COMMITTER_NAME'] = name
            os.environ['GIT_COMMITTER_EMAIL'] = email

        cmd = '--bare read-tree %s' % commit.parent
        self.__get_git_data(cmd)

        print commit.files

        cmd = '--bare update-index --add --index-info'
        ui = GitData(self.config.location, cmd)
        for path, data in commit.files.items():
            sha = data['sha1']
            props = data.get('props', {})
            mode = '100644'

            if 'svn:special' in props:
                mode, sha = self.__translate_special(sha)
                if sha is None:
                    print 'some unknown type of svn special object has been ' \
                          'sent!'
                    continue

            if 'svn:executable' in props:
                mode = '100755'

            ui.write('%s %s\t%s\n' % (mode, sha, path))
        ui.close()

        cmd = '--bare write-tree'
        tree = self.__get_git_data(cmd)[0]

        cmd = '--bare commit-tree %s -p %s' % (tree, commit.parent)
        ct = GitData(self.config.location, cmd)
        ct.write(msg)
        ct.close_stdin()
        commit_sha = ct.read().strip()
        ct.close()

        ref = 'refs/svnserver/%s' % uuid.uuid4()
        cmd = '--bare update-ref -m "svn commit" %s %s' % (ref, commit_sha)
        self.__get_git_data(cmd)

        cmd = 'push . %s:%s' % (ref, commit.ref)
        self.__get_git_data(cmd)

        cmd = '--bare update-ref -d %s %s' % (ref, commit_sha)
        self.__get_git_data(cmd)

        ref, rev = self.map.get_ref_rev(commit_sha)
        tree, parents, name, email, date, msg = self.__commit_info(commit_sha)

        os.environ['GIT_INDEX_FILE'] = orig_index_file
        os.environ['GIT_AUTHOR_NAME'] = orig_author[0]
        os.environ['GIT_AUTHOR_EMAIL'] = orig_author[0]
        os.environ['GIT_COMMITTER_NAME'] = orig_committer[0]
        os.environ['GIT_COMMITTER_EMAIL'] = orig_committer[0]

        return rev, date, email, ""

    def abort_commit(self, commit):
        print "abort commit ..."
        pass

    def get_auth(self):
        return self.auth_db