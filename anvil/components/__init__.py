# vim: tabstop=4 shiftwidth=4 softtabstop=4

#    Copyright (C) 2012 Yahoo! Inc. All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

# vim: tabstop=4 shiftwidth=4 softtabstop=4

#    Copyright (C) 2012 Yahoo! Inc. All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import functools
import os
import re
import weakref

from anvil import cfg
from anvil import colorizer
from anvil import component
from anvil import decorators
from anvil import downloader as down
from anvil import exceptions as excp
from anvil import importer
from anvil import log as logging
from anvil import packager
from anvil import patcher
from anvil import shell as sh
from anvil import trace as tr
from anvil import utils

from anvil.packaging import pip

from anvil.packaging.helpers import pip_helper

LOG = logging.getLogger(__name__)

####
#### STATUS CONSTANTS
####
STATUS_INSTALLED = 'installed'
STATUS_STARTED = "started"
STATUS_STOPPED = "stopped"
STATUS_UNKNOWN = "unknown"


class ProgramStatus(object):
    def __init__(self, status, name=None, details=''):
        self.name = name
        self.status = status
        self.details = details

####
#### Utils...
####

# Cache of accessed packagers
_PACKAGERS = {}


def make_packager(package, default_class, **kwargs):
    packager_name = package.get('packager_name') or ''
    packager_name = packager_name.strip()
    if packager_name:
        packager_cls = importer.import_entry_point(packager_name)
    else:
        packager_cls = default_class
    if packager_cls in _PACKAGERS:
        return _PACKAGERS[packager_cls]
    p = packager_cls(**kwargs)
    _PACKAGERS[packager_cls] = p
    return p


# Remove any private keys from a package dictionary
def filter_package(pkg):
    n_pkg = {}
    for (k, v) in pkg.items():
        if not k or k.startswith("_"):
            continue
        else:
            n_pkg[k] = v
    return n_pkg

####
#### INSTALL CLASSES
####


class PkgInstallComponent(component.Component):
    def __init__(self, *args, **kargs):
        component.Component.__init__(self, *args, **kargs)
        trace_fn = tr.trace_filename(self.get_option('trace_dir'), 'created')
        self.tracewriter = tr.TraceWriter(trace_fn, break_if_there=False)

    def _get_download_config(self):
        return None

    def _get_download_location(self):
        key = self._get_download_config()
        if not key:
            return (None, None)
        uri = self.get_option(key, default_value='').strip()
        if not uri:
            raise ValueError(("Could not find uri in config to download "
                              "from option %s") % (key))
        return (uri, self.get_option('app_dir'))

    def download(self):
        (from_uri, target_dir) = self._get_download_location()
        if not from_uri and not target_dir:
            return []
        else:
            uris = [from_uri]
            utils.log_iterable(uris, logger=LOG,
                               header="Downloading from %s uris" % (len(uris)))
            sh.mkdirslist(target_dir, tracewriter=self.tracewriter)
            # This is used to delete what is downloaded (done before
            # fetching to ensure its cleaned up even on download failures)
            self.tracewriter.download_happened(target_dir, from_uri)
            fetcher = down.GitDownloader(self.distro, from_uri, target_dir)
            fetcher.download()
            return uris

    def patch(self, section):
        what_patches = self.get_option('patches', section)
        (_from_uri, target_dir) = self._get_download_location()
        patcher.apply_patches(what_patches, target_dir)

    def config_params(self, config_fn):
        mp = dict(self.params)
        if config_fn:
            mp['CONFIG_FN'] = config_fn
        return mp

    @property
    def packages(self):
        pkg_list = self.get_option('packages', default_value=[])
        if not pkg_list:
            pkg_list = []
        for name, values in self.subsystems.items():
            if 'packages' in values:
                LOG.debug("Extending package list with packages for subsystem: %r", name)
                pkg_list.extend(values.get('packages'))
        return pkg_list

    def install(self):
        LOG.debug('Preparing to install packages for: %r', self.name)
        pkgs = self.packages
        if pkgs:
            pkg_names = [p['name'] for p in pkgs]
            utils.log_iterable(pkg_names, logger=LOG,
                               header="Setting up %s distribution packages" % (len(pkg_names)))
            with utils.progress_bar('Installing', len(pkgs)) as p_bar:
                for (i, p) in enumerate(pkgs):
                    installer = make_packager(p, self.distro.package_manager_class,
                                              distro=self.distro)
                    installer.install(p)
                    # Mark that this happened so that we can uninstall it
                    self.tracewriter.package_installed(filter_package(p))
                    p_bar.update(i + 1)

    def pre_install(self):
        pkgs = self.packages
        for p in pkgs:
            installer = make_packager(p, self.distro.package_manager_class,
                                      distro=self.distro)
            installer.pre_install(p, self.params)

    def post_install(self):
        pkgs = self.packages
        for p in pkgs:
            installer = make_packager(p, self.distro.package_manager_class,
                                      distro=self.distro)
            installer.post_install(p, self.params)

    @property
    def config_files(self):
        return []

    def _config_adjust(self, contents, config_fn):
        return contents

    def target_config(self, config_fn):
        return sh.joinpths(self.get_option('cfg_dir'), config_fn)

    def source_config(self, config_fn):
        return utils.load_template(self.name, config_fn)

    @property
    def link_dir(self):
        link_dir_base = self.distro.get_command_config('base_link_dir')
        return sh.joinpths(link_dir_base, self.name)

    @property
    def symlinks(self):
        links = {}
        for fn in self.config_files:
            source_fn = self.target_config(fn)
            links[source_fn] = [sh.joinpths(self.link_dir, fn)]
        return links

    def _config_param_replace(self, config_fn, contents, parameters):
        return utils.expand_template(contents, parameters)

    def _configure_files(self):
        config_fns = self.config_files
        if config_fns:
            utils.log_iterable(config_fns, logger=LOG,
                               header="Configuring %s files" % (len(config_fns)))
            for fn in config_fns:
                tgt_fn = self.target_config(fn)
                sh.mkdirslist(sh.dirname(tgt_fn), tracewriter=self.tracewriter)
                (source_fn, contents) = self.source_config(fn)
                LOG.debug("Configuring file %s ---> %s.", (source_fn), (tgt_fn))
                contents = self._config_param_replace(fn, contents, self.config_params(fn))
                contents = self._config_adjust(contents, fn)
                sh.write_file(tgt_fn, contents, tracewriter=self.tracewriter)
        return len(config_fns)

    def _configure_symlinks(self):
        links = self.symlinks
        if not links:
            return 0
        # This sort happens so that we link in the correct order
        # although it might not matter. Either way. We ensure that the right
        # order happens. Ie /etc/blah link runs before /etc/blah/blah
        link_srcs = sorted(links.keys())
        link_srcs.reverse()
        link_nice = []
        for source in link_srcs:
            links_to_be = links[source]
            for link in links_to_be:
                link_nice.append("%s => %s" % (link, source))
        utils.log_iterable(link_nice, logger=LOG,
                           header="Creating %s sym-links" % (len(link_nice)))
        links_made = 0
        for source in link_srcs:
            links_to_be = links[source]
            for link in links_to_be:
                try:
                    LOG.debug("Symlinking %s to %s.", link, source)
                    sh.symlink(source, link, tracewriter=self.tracewriter)
                    links_made += 1
                except (IOError, OSError) as e:
                    LOG.warn("Symlinking %s to %s failed: %s", colorizer.quote(link), colorizer.quote(source), e)
        return links_made

    def configure(self):
        return self._configure_files() + self._configure_symlinks()


class PythonInstallComponent(PkgInstallComponent):
    def __init__(self, *args, **kargs):
        PkgInstallComponent.__init__(self, *args, **kargs)
        self.requires_files = [
            sh.joinpths(self.get_option('app_dir'), 'tools', 'pip-requires'),
        ]
        if self.get_bool_option('use_tests_requires', default_value=True):
            self.requires_files.append(sh.joinpths(self.get_option('app_dir'), 'tools', 'test-requires'))

    def _get_download_config(self):
        return 'get_from'

    @property
    def python_directories(self):
        py_dirs = {}
        app_dir = self.get_option('app_dir')
        if sh.isdir(app_dir):
            py_dirs[self.name] = app_dir
        return py_dirs

    @property
    def packages(self):
        pkg_list = super(PythonInstallComponent, self).packages
        if not pkg_list:
            pkg_list = []
        pkg_list.extend(self._get_mapped_packages())
        return pkg_list

    @property
    def pips_to_packages(self):
        pip_pkg_list = self.get_option('pip_to_package', default_value=[])
        if not pip_pkg_list:
            pip_pkg_list = []
        return pip_pkg_list

    @property
    def pip_requires(self):
        all_pips = []
        for fn in self.requires_files:
            all_pips.extend(self._extract_pip_requires(fn))
        return all_pips

    def _match_pip_requires(self, pip_req):

        def pip_use(who, there_pip):
            if there_pip.key != pip_req.key:
                return False
            if not len(pip_req.specs):
                # No version/restrictions specified
                return True
            there_version = None
            if there_pip.version is not None:
                there_version = str(there_pip.version)
            if there_version in pip_req:
                return True
            # Different possibly incompat. versions found...
            if there_version is None:
                # Assume pip will install the correct version anyway
                if who != self.name:
                    msg = ("Component %r asked for package '%s'"
                           " and '%s' is being selected from %r instead...")
                    LOG.debug(msg, self.name, pip_req, there_pip, who)
                return True
            else:
                if who != self.name:
                    msg = ("Component %r provides package '%s'"
                           " but '%s' is being asked for by %r instead...")
                    LOG.warn(msg, who, there_pip, pip_req, self.name)
                return False

        LOG.debug("Attempting to find who satisfies pip requirement '%s'", pip_req)

        # Try to find it in anyones pip -> pkg list
        all_pip_2_pkgs = {
            self.name: self.pips_to_packages,
        }
        # Gather them all (but only if they activate before me)
        # since if they activate after, we can't depend on it
        # to satisfy our requirement...
        for (name, c) in self.instances.items():
            if c is self or not c.activated:
                continue
            if isinstance(c, (PythonInstallComponent)):
                all_pip_2_pkgs[name] = c.pips_to_packages
        for (who, pips_2_pkgs) in all_pip_2_pkgs.items():
            for pip_info in pips_2_pkgs:
                there_pip = pip.extract_requirement(pip_info)
                if not pip_use(who, there_pip):
                    continue
                LOG.debug("Matched pip->pkg '%s' from component %r", there_pip, who)
                return (dict(pip_info.get('package')), False)

        # Ok nobody had it in a pip->pkg mapping
        # but see if they had it in there pip collection
        all_pips = {
            self.name: self._base_pips(),  # Use base pips to avoid recursion...
        }
        for (name, c) in self.instances.items():
            if not c.activated or c is self:
                continue
            if isinstance(c, (PythonInstallComponent)):
                all_pips[name] = c._base_pips()  # pylint: disable=W0212
        for (who, there_pips) in all_pips.items():
            for pip_info in there_pips:
                there_pip = pip.extract_requirement(pip_info)
                if not pip_use(who, there_pip):
                    continue
                LOG.debug("Matched pip '%s' from component %r", there_pip, who)
                return (dict(pip_info), True)
        return (None, False)

    def _get_mapped_packages(self):
        add_on_pkgs = []
        all_pips = self.pip_requires
        for details in all_pips:
            pkg_info = details['package']
            from_pip = details['from_pip']
            if from_pip or not pkg_info:
                continue
            # Keep the initial requirement
            pkg_info = dict(pkg_info)
            pkg_info['__requirement'] = details['requirement']
            add_on_pkgs.append(pkg_info)
        return add_on_pkgs

    def _get_mapped_pips(self):
        add_on_pips = []
        all_pips = self.pip_requires
        for details in all_pips:
            pkg_info = details['package']
            from_pip = details['from_pip']
            if not from_pip or not pkg_info:
                continue
            # Keep the initial requirement
            pkg_info = dict(pkg_info)
            pkg_info['__requirement'] = details['requirement']
            add_on_pips.append(pkg_info)
        return add_on_pips

    def _base_pips(self):
        pip_list = self.get_option('pips', default_value=[])
        if not pip_list:
            pip_list = []
        for (name, values) in self.subsystems.items():
            if 'pips' in values:
                LOG.debug("Extending pip list with pips for subsystem: %r" % (name))
                pip_list.extend(values.get('pips'))
        return pip_list

    @property
    def pips(self):
        pip_list = self._base_pips()
        pip_list.extend(self._get_mapped_pips())
        return pip_list

    def _install_pips(self):
        pips = self.pips
        if pips:
            pip_names = [p['name'] for p in pips]
            utils.log_iterable(pip_names, logger=LOG,
                               header="Setting up %s python packages" % (len(pip_names)))
            with utils.progress_bar('Installing', len(pips)) as p_bar:
                for (i, p) in enumerate(pips):
                    installer = make_packager(p, pip.Packager,
                                              distro=self.distro)
                    installer.install(p)
                    # Note that we did it so that we can remove it...
                    self.tracewriter.pip_installed(filter_package(p))
                    p_bar.update(i + 1)

    def _clean_pip_requires(self):
        # Fixup these files if they exist (sometimes they have 'junk' in them)
        req_fns = []
        for fn in self.requires_files:
            if not sh.isfile(fn):
                continue
            req_fns.append(fn)
        if req_fns:
            utils.log_iterable(req_fns, logger=LOG,
                               header="Adjusting %s pip 'requires' files" % (len(req_fns)))
            for fn in req_fns:
                new_lines = []
                for line in sh.load_file(fn).splitlines():
                    s_line = line.strip()
                    if len(s_line) == 0:
                        continue
                    elif s_line.startswith("#"):
                        new_lines.append(s_line)
                    elif not self._filter_pip_requires_line(fn, s_line):
                        new_lines.append(("# %s" % (s_line)))
                    else:
                        new_lines.append(s_line)
                contents = "# Cleaned on %s\n\n%s\n" % (utils.iso8601(), "\n".join(new_lines))
                sh.write_file_and_backup(fn, contents)
        return len(req_fns)

    def _filter_pip_requires_line(self, fn, line):
        # Return none to filter or the line itself to leave alone...
        return line

    def pre_install(self):
        self._verify_pip_requires()
        PkgInstallComponent.pre_install(self)
        for p in self.pips:
            installer = make_packager(p, pip.Packager,
                                      distro=self.distro)
            installer.pre_install(p, self.params)

    def post_install(self):
        PkgInstallComponent.post_install(self)
        for p in self.pips:
            installer = make_packager(p, pip.Packager,
                                      distro=self.distro)
            installer.post_install(p, self.params)

    def _install_python_setups(self):
        py_dirs = self.python_directories
        if py_dirs:
            real_dirs = {}
            for (name, wkdir) in py_dirs.items():
                real_dirs[name] = wkdir
                if not real_dirs[name]:
                    real_dirs[name] = self.get_option('app_dir')
            utils.log_iterable(real_dirs.values(), logger=LOG,
                               header="Setting up %s python directories" % (len(real_dirs)))
            setup_cmd = self.distro.get_command('python', 'setup')
            for (name, working_dir) in real_dirs.items():
                sh.mkdirslist(working_dir, tracewriter=self.tracewriter)
                setup_fn = sh.joinpths(self.get_option('trace_dir'), "%s.python.setup" % (name))
                sh.execute(*setup_cmd, cwd=working_dir, run_as_root=True,
                           stderr_fn='%s.stderr' % (setup_fn),
                           stdout_fn='%s.stdout' % (setup_fn),
                           tracewriter=self.tracewriter)
                self.tracewriter.py_installed(name, working_dir)

    def _python_install(self):
        self._install_pips()
        self._install_python_setups()

    @decorators.memoized
    def _extract_pip_requires(self, fn):
        if not sh.isfile(fn):
            return []
        LOG.debug("Resolving dependencies from %s.", colorizer.quote(fn))
        pips_needed = pip_helper.parse_requirements(sh.load_file(fn))
        matchings = []
        for req in pips_needed:
            (pkg_info, from_pip) = self._match_pip_requires(req)
            matchings.append({
                'requirement': req,
                'package': pkg_info,
                'from_pip': from_pip,
                'needed_by': fn,
            })
        return matchings

    def _verify_pip_requires(self):
        all_pips = self.pip_requires
        for details in all_pips:
            req = details['requirement']
            needed_by = details['needed_by']
            pkg_info = details['package']
            if not pkg_info:
                raise excp.DependencyException(("Pip dependency '%s' needed by '%s' is not translatable to a listed"
                                                " (from this or previously activated components) pip package"
                                                ' or a pip->package mapping!') % (req, needed_by))

    def install(self):
        PkgInstallComponent.install(self)
        self._python_install()

    def configure(self):
        configured_am = PkgInstallComponent.configure(self)
        configured_am += self._clean_pip_requires()
        return configured_am


####
#### RUNTIME CLASSES
####

class ProgramRuntime(component.Component):
    @property
    def apps_to_start(self):
        return []

    def app_options(self, app_name):
        return []

    def app_params(self, app_name):
        mp = dict(self.params)
        if app_name:
            mp['APP_NAME'] = app_name
        return mp

    def restart(self):
        return 0

    def post_start(self):
        pass

    def pre_start(self):
        pass

    def status(self):
        return []

    def start(self):
        return 0

    def stop(self):
        return 0

    def wait_active(self, between_wait=1, max_attempts=5):
        rt_name = self.name
        num_started = len(self.apps_to_start)
        if not num_started:
            raise excp.StartException("No %r programs started, can not wait for them to become active..." % (rt_name))

        def waiter(try_num):
            LOG.info("Waiting %s seconds for component %s programs to start.", between_wait, colorizer.quote(rt_name))
            LOG.info("Please wait...")
            sh.sleep(between_wait)

        for i in range(0, max_attempts):
            statii = self.status()
            if len(statii) == num_started:
                not_worked = []
                for p_status in statii:
                    if p_status.status != STATUS_STARTED:
                        not_worked.append(p_status)
                if len(not_worked) == 0:
                    return
            waiter(i + 1)

        tot_time = max(0, between_wait * max_attempts)
        raise excp.StartException("Failed waiting %s seconds for component %r programs to become active..."
                                  % (tot_time, rt_name))


class EmptyRuntime(ProgramRuntime):
    pass


class PythonRuntime(ProgramRuntime):
    def __init__(self, *args, **kargs):
        ProgramRuntime.__init__(self, *args, **kargs)
        trace_fn = tr.trace_filename(self.get_option('trace_dir'), 'start')
        self.tracewriter = tr.TraceWriter(trace_fn, break_if_there=True)
        self.tracereader = tr.TraceReader(trace_fn)

    def start(self):
        # Select how we are going to start it
        run_type = self.get_option("run_type", default_value='anvil.runners.fork:ForkRunner')
        starter = importer.construct_entry_point(run_type, self)
        am_started = 0
        for app_info in self.apps_to_start:
            self._start_app(app_info, run_type, starter)
            self._post_app_start(app_info)
            am_started += 1
        return am_started

    def _start_app(self, app_info, run_type, starter):
        app_name = app_info["name"]
        app_pth = app_info.get("path", app_name)
        app_dir = app_info.get("app_dir", self.get_option('app_dir'))
        app_options = self.app_options(app_name)
        app_params = self.app_params(app_name)
        program_opts = [utils.expand_template(c, app_params) for c in app_options]
        LOG.debug("Starting %r using %r", app_name, starter)
        details_fn = starter.start(app_name, app_pth=app_pth, app_dir=app_dir, opts=program_opts)
        LOG.info("Started sub-program %s.", colorizer.quote(app_name))
        # This trace is used to locate details about what/how to stop
        self.tracewriter.app_started(app_name, details_fn, run_type)

    def _post_app_start(self, app_info):
        if 'sleep_time' in app_info:
            LOG.info("%s requested a %s second sleep time, please wait...",
                     colorizer.quote(app_info.get('name')), app_info.get('sleep_time'))
            sh.sleep(int(app_info.get('sleep_time')))

    def _locate_investigators(self, apps_started):
        investigator_created = {}
        to_investigate = []
        for (app_name, _trace_fn, run_type) in apps_started:
            investigator = investigator_created.get(run_type)
            if investigator is None:
                try:
                    investigator = importer.construct_entry_point(run_type, self)
                    investigator_created[run_type] = investigator
                except RuntimeError as e:
                    LOG.warn("Could not load class %s which should be used to investigate %s: %s",
                             colorizer.quote(run_type), colorizer.quote(app_name), e)
                    continue
            to_investigate.append((app_name, investigator))
        return to_investigate

    def stop(self):
        # Anything to stop??
        killed_am = 0
        apps_started = 0
        try:
            apps_started = self.tracereader.apps_started()
        except excp.NoTraceException:
            pass
        if not apps_started:
            return killed_am
        to_kill = self._locate_investigators(apps_started)
        for (app_name, handler) in to_kill:
            handler.stop(app_name)
            killed_am += 1
        if len(apps_started) == killed_am:
            sh.unlink(self.tracereader.filename())
        return killed_am

    def status(self):
        statii = []
        apps_started = None
        try:
            apps_started = self.tracereader.apps_started()
        except excp.NoTraceException:
            pass
        if not apps_started:
            return statii
        to_check = self._locate_investigators(apps_started)
        for (name, handler) in to_check:
            (status, details) = handler.status(name)
            statii.append(ProgramStatus(name=name,
                                        status=status,
                                        details=details))
        return statii


####
#### UNINSTALL CLASSES
####

class PkgUninstallComponent(component.Component):
    def __init__(self, *args, **kargs):
        component.Component.__init__(self, *args, **kargs)
        trace_fn = tr.trace_filename(self.get_option('trace_dir'), 'created')
        self.tracereader = tr.TraceReader(trace_fn)
        self.purge_packages = kargs.get('purge_packages')

    def unconfigure(self):
        self._unconfigure_links()

    def _unconfigure_links(self):
        sym_files = self.tracereader.symlinks_made()
        if sym_files:
            utils.log_iterable(sym_files, logger=LOG,
                               header="Removing %s symlink files" % (len(sym_files)))
            for fn in sym_files:
                sh.unlink(fn, run_as_root=True)

    def uninstall(self):
        self._uninstall_pkgs()
        self._uninstall_files()

    def post_uninstall(self):
        self._uninstall_dirs()

    def pre_uninstall(self):
        pass

    def _uninstall_pkgs(self):
        pkgs = self.tracereader.packages_installed()
        if pkgs:
            pkg_names = set([p['name'] for p in pkgs])
            utils.log_iterable(pkg_names, logger=LOG,
                               header="Potentially removing %s distribution packages" % (len(pkg_names)))
            which_removed = []
            with utils.progress_bar('Uninstalling', len(pkgs), reverse=True) as p_bar:
                for (i, p) in enumerate(pkgs):
                    uninstaller = make_packager(p, self.distro.package_manager_class,
                                                distro=self.distro,
                                                remove_default=self.purge_packages)
                    if uninstaller.remove(p):
                        which_removed.append(p['name'])
                    p_bar.update(i + 1)
            utils.log_iterable(which_removed, logger=LOG,
                               header="Actually removed %s distribution packages" % (len(which_removed)))

    def _uninstall_files(self):
        files_touched = self.tracereader.files_touched()
        if files_touched:
            utils.log_iterable(files_touched, logger=LOG,
                               header="Removing %s miscellaneous files" % (len(files_touched)))
            for fn in files_touched:
                sh.unlink(fn, run_as_root=True)

    def _uninstall_dirs(self):
        dirs_made = self.tracereader.dirs_made()
        dirs_alive = filter(sh.isdir, dirs_made)
        if dirs_alive:
            utils.log_iterable(dirs_alive, logger=LOG,
                               header="Removing %s created directories" % (len(dirs_alive)))
            for dir_name in dirs_alive:
                sh.deldir(dir_name, run_as_root=True)


class PythonUninstallComponent(PkgUninstallComponent):

    def uninstall(self):
        self._uninstall_python()
        self._uninstall_pips()
        PkgUninstallComponent.uninstall(self)

    def _uninstall_pips(self):
        pips = self.tracereader.pips_installed()
        if pips:
            pip_names = [p['name'] for p in pips]
            utils.log_iterable(pip_names, logger=LOG,
                               header="Potentially removing %s python packages" % (len(pip_names)))
            which_removed = []
            with utils.progress_bar('Uninstalling', len(pips), reverse=True) as p_bar:
                for (i, p) in enumerate(pips):
                    try:
                        uninstaller = make_packager(p, pip.Packager,
                                                    distro=self.distro,
                                                    remove_default=self.purge_packages)
                        if uninstaller.remove(p):
                            which_removed.append(p['name'])
                    except excp.ProcessExecutionError as e:
                        # NOTE(harlowja): pip seems to die if a pkg isn't there even in quiet mode
                        combined = (str(e.stderr) + str(e.stdout))
                        if not re.search(r"not\s+installed", combined, re.I):
                            raise
                    p_bar.update(i + 1)
            utils.log_iterable(which_removed, logger=LOG,
                               header="Actually removed %s python packages" % (len(which_removed)))

    def _uninstall_python(self):
        py_listing = self.tracereader.py_listing()
        if py_listing:
            py_listing_dirs = set()
            for (_name, where) in py_listing:
                py_listing_dirs.add(where)
            utils.log_iterable(py_listing_dirs, logger=LOG,
                               header="Uninstalling %s python setups" % (len(py_listing_dirs)))
            unsetup_cmd = self.distro.get_command('python', 'unsetup')
            for where in py_listing_dirs:
                if sh.isdir(where):
                    sh.execute(*unsetup_cmd, cwd=where, run_as_root=True)
                else:
                    LOG.warn("No python directory found at %s - skipping", colorizer.quote(where, quote_color='red'))


####
#### TESTING CLASSES
####


class EmptyTestingComponent(component.Component):
    def run_tests(self):
        return


class PythonTestingComponent(component.Component):
    def __init__(self, *args, **kargs):
        component.Component.__init__(self, *args, **kargs)
        self.helper = pip_helper.Helper(self.distro)

    def _get_test_exclusions(self):
        return []

    def _use_run_tests(self):
        return True

    def _get_test_command(self):
        # See: http://docs.openstack.org/developer/nova/devref/unit_tests.html
        # And: http://wiki.openstack.org/ProjectTestingInterface
        app_dir = self.get_option('app_dir')
        if sh.isfile(sh.joinpths(app_dir, 'run_tests.sh')) and self._use_run_tests():
            cmd = [sh.joinpths(app_dir, 'run_tests.sh'), '-N']
            if not self._use_pep8():
                cmd.append('--no-pep8')
        else:
            # Assume tox is being used, which we can't use directly
            # since anvil doesn't really do venv stuff (its meant to avoid those...)
            cmd = ['nosetests']
        # See: $ man nosetests
        if self.get_bool_option("verbose", default_value=False):
            cmd.append('--nologcapture')
        for e in self._get_test_exclusions():
            cmd.append('--exclude=%s' % (e))
        return cmd

    def _use_pep8(self):
        # Seems like the varying versions are borking pep8 from working...
        i_sibling = self.siblings.get('install')
        # Check if whats installed actually matches
        pep8_wanted = None
        if isinstance(i_sibling, (PythonInstallComponent)):
            for p in i_sibling.pip_requires:
                req = p['__requirement']
                if req.key == "pep8":
                    pep8_wanted = req
                    break
        if not pep8_wanted:
            # Doesn't matter since its not wanted anyway
            return True
        pep8_there = self.helper.get_installed('pep8')
        if not pep8_there:
            # Hard to use it if it isn't there...
            LOG.warn("Pep8 version mismatch, none is installed but %s is wanting %s",
                     self.name, pep8_wanted)
            return False
        if not (pep8_there == pep8_wanted):
            # Versions not matching, this is causes pep8 to puke when it doesn't need to
            # so skip it from running in the first place...
            LOG.warn("Pep8 version mismatch, installed is %s but %s is applying %s",
                     pep8_there, self.name, pep8_wanted)
            return False
        return self.get_bool_option('use_pep8', default_value=True)

    def _get_env(self):
        env_addons = {}
        tox_fn = sh.joinpths(self.get_option('app_dir'), 'tox.ini')
        if sh.isfile(tox_fn):
            # Suck out some settings from the tox file
            try:
                tox_cfg = cfg.BuiltinConfigParser(fns=[tox_fn])
                env_values = tox_cfg.get('testenv', 'setenv') or ''
                for env_line in env_values.splitlines():
                    env_line = env_line.strip()
                    env_line = env_line.split("#")[0].strip()
                    if not env_line:
                        continue
                    env_entry = env_line.split('=', 1)
                    if len(env_entry) == 2:
                        (name, value) = env_entry
                        name = name.strip()
                        value = value.strip()
                        if name.lower() != 'virtual_env':
                            env_addons[name] = value
                if env_addons:
                    LOG.debug("From %s we read in %s environment settings:", tox_fn, len(env_addons))
                    utils.log_object(env_addons, logger=LOG, level=logging.DEBUG)
            except IOError:
                pass
        return env_addons

    def run_tests(self):
        app_dir = self.get_option('app_dir')
        if not sh.isdir(app_dir):
            LOG.warn("Unable to find application directory at %s, can not run %s tests.",
                     colorizer.quote(app_dir), colorizer.quote(self.name))
            return
        cmd = self._get_test_command()
        env = self._get_env()
        with open(os.devnull, 'wb') as null_fh:
            if self.get_bool_option("tests_verbose", default_value=False):
                null_fh = None
            sh.execute(*cmd, stdout_fh=None, stderr_fh=null_fh,
                       cwd=app_dir, env_overrides=env)


####
#### PACKAGING CLASSES
####

class EmptyPackagingComponent(component.Component):
    def package(self):
        return None
