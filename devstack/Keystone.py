# vim: tabstop=4 shiftwidth=4 softtabstop=4

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

import os
import os.path

import Pip
from Util import (KEYSTONE, 
                  CONFIG_DIR, STACK_CONFIG_DIR,
                  NOVA, GLANCE, SWIFT,
                  get_pkg_list, get_pip_list,
                  get_dbdsn,
                  param_replace, get_host_ip,
                  execute_template)
import Logger
import Downloader
import Db
from Trace import (TraceWriter, TraceReader,
                    touch_trace,
                    IN_TRACE, PY_TRACE)
from Shell import (execute, mkdirslist, write_file,
                    load_file, joinpths, touch_file,
                    unlink, deldir)
from Component import (ComponentBase, RuntimeComponent,
                       UninstallComponent, InstallComponent)

LOG = Logger.getLogger("install.keystone")

TYPE = KEYSTONE
PY_INSTALL = ['python', 'setup.py', 'develop']
PY_UNINSTALL = ['python', 'setup.py', 'develop', '--uninstall']
ROOT_CONF = "keystone.conf"
CONFIGS = [ROOT_CONF]
BIN_DIR = "bin"
DB_NAME = "keystone"


class KeystoneBase(ComponentBase):
    def __init__(self, *args, **kargs):
        ComponentBase.__init__(self, TYPE, *args, **kargs)
        self.cfgdir = joinpths(self.appdir, CONFIG_DIR)
        self.bindir = joinpths(self.appdir, BIN_DIR)


class KeystoneUninstaller(KeystoneBase, UninstallComponent):
    def __init__(self, *args, **kargs):
        KeystoneBase.__init__(self, *args, **kargs)
        self.tracereader = TraceReader(self.tracedir, IN_TRACE)

    def unconfigure(self):
        #get rid of all files configured
        cfgfiles = self.tracereader.files_configured()
        if(len(cfgfiles)):
            LOG.info("Removing %s configuration files" % (len(cfgfiles)))
            for fn in cfgfiles:
                if(len(fn)):
                    unlink(fn)
                    LOG.info("Removed %s" % (fn))

    def uninstall(self):
        #clean out removeable packages
        pkgsfull = self.tracereader.packages_installed()
        if(len(pkgsfull)):
            LOG.info("Potentially removing %s packages" % (len(pkgsfull)))
            self.packager.remove_batch(pkgsfull)
        #clean out pips
        pipsfull = self.tracereader.pips_installed()
        if(len(pipsfull)):
            LOG.info("Potentially removing %s pips" % (len(pipsfull)))
            Pip.uninstall(pipsfull)
        #clean out files touched
        filestouched = self.tracereader.files_touched()
        if(len(filestouched)):
            LOG.info("Removing %s touched files" % (len(filestouched)))
            for fn in filestouched:
                if(len(fn)):
                    unlink(fn)
                    LOG.info("Removed %s" % (fn))
        #undevelop python???
        #how should this be done??
        pylisting = self.tracereader.py_listing()
        if(pylisting != None):
            execute(*PY_UNINSTALL, cwd=self.appdir, run_as_root=True)
        #clean out dirs created
        dirsmade = self.tracereader.dirs_made()
        if(len(dirsmade)):
            LOG.info("Removing %s created directories" % (len(dirsmade)))
            for dirname in dirsmade:
                deldir(dirname)
                LOG.info("Removed %s" % (dirname))


class KeystoneInstaller(KeystoneBase, InstallComponent):
    def __init__(self, *args, **kargs):
        KeystoneBase.__init__(self, *args, **kargs)
        self.gitloc = self.cfg.get("git", "keystone_repo")
        self.brch = self.cfg.get("git", "keystone_branch")
        self.tracewriter = TraceWriter(self.tracedir, IN_TRACE)

    def download(self):
        dirsmade = Downloader.download(self.appdir, self.gitloc, self.brch)
        #this trace isn't used yet but could be
        self.tracewriter.downloaded(self.appdir, self.gitloc)
        #this trace is used to remove the dirs created
        self.tracewriter.dir_made(*dirsmade)
        return self.tracedir

    def _do_install(self, pkgs):
        LOG.debug("Installing packages %s" % (", ".join(pkgs.keys())))
        self.packager.pre_install(pkgs)
        self.packager.install_batch(pkgs)
        self.packager.post_install(pkgs)

    def install(self):
        #install needed pkgs
        pkgs = get_pkg_list(self.distro, TYPE)
        if(len(pkgs)):
            #do the install
            self._do_install(pkgs)
            #this trace is used to remove the pkgs installed
            for name in pkgs.keys():
                self.tracewriter.package_install(name, pkgs.get(name))
        #install the needed pips
        pips = get_pip_list(self.distro, TYPE)
        if(len(pips)):
            Pip.install(pips)
            #this trace is used to remove the pips installed
            for name in pips.keys():
                self.tracewriter.pip_install(name, pips.get(name))
        #this trace is used to remove the dirs created
        dirsmade = mkdirslist(self.tracedir)
        self.tracewriter.dir_made(*dirsmade)
        recordwhere = touch_trace(self.tracedir, PY_TRACE)
        #this trace is used to remove the trace created
        self.tracewriter.py_install(recordwhere)
        (sysout, stderr) = execute(*PY_INSTALL, cwd=self.appdir, run_as_root=True)
        write_file(recordwhere, sysout)
        #adjust db
        self._setup_db()
        #setup any data
        self._setup_data()
        return self.tracedir

    def configure(self):
        dirsmade = mkdirslist(self.cfgdir)
        self.tracewriter.dir_made(*dirsmade)
        for fn in CONFIGS:
            sourcefn = joinpths(STACK_CONFIG_DIR, TYPE, fn)
            tgtfn = joinpths(self.cfgdir, fn)
            LOG.info("Configuring template file %s" % (sourcefn))
            contents = load_file(sourcefn)
            pmap = self._get_param_map()
            LOG.info("Replacing parameters in file %s" % (sourcefn))
            LOG.debug("Replacements = %s" % (pmap))
            contents = param_replace(contents, pmap)
            LOG.debug("Applying side-effects of param replacement for template %s" % (sourcefn))
            self._config_apply(contents, fn)
            LOG.info("Writing configuration file %s" % (tgtfn))
            write_file(tgtfn, contents)
            #this trace is used to remove the files configured
            self.tracewriter.cfg_write(tgtfn)
        return self.tracedir

    def _setup_db(self):
        Db.drop_db(self.cfg, DB_NAME)
        Db.create_db(self.cfg, DB_NAME)

    def _setup_data(self):
        params = self._get_param_map()
        cmds = _keystone_setup_cmds(self.othercomponents)
        execute_template(*cmds, params=params, ignore_missing=True)

    def _config_apply(self, contents, fn):
        lines = contents.splitlines()
        for line in lines:
            cleaned = line.strip()
            if(len(cleaned) == 0 or cleaned[0] == '#' or cleaned[0] == '['):
                #not useful to examine these
                continue
            pieces = cleaned.split("=", 1)
            if(len(pieces) != 2):
                continue
            key = pieces[0].strip()
            val = pieces[1].strip()
            if(len(key) == 0 or len(val) == 0):
                continue
            #now we take special actions
            if(key == 'log_file'):
                # Ensure that we can write to the log file
                dirname = os.path.dirname(val)
                if(len(dirname)):
                    dirsmade = mkdirslist(dirname)
                    # This trace is used to remove the dirs created
                    self.tracewriter.dir_made(*dirsmade)
                # Destroy then recreate it
                unlink(val)
                touch_file(val)
                self.tracewriter.file_touched(val)

    def _get_param_map(self):
        #these be used to fill in the configuration/cmds +
        #params with actual values
        mp = dict()
        mp['DEST'] = self.appdir
        mp['SQL_CONN'] = get_dbdsn(self.cfg, DB_NAME)
        mp['ADMIN_PASSWORD'] = self.cfg.getpw('passwords', 'horizon_keystone_admin')
        mp['HOST_IP'] = get_host_ip(self.cfg)
        mp['SERVICE_TOKEN'] = self.cfg.getpw("passwords", "service_token")
        mp['BIN_DIR'] = self.bindir
        mp['CONFIG_FILE'] = joinpths(self.cfgdir, ROOT_CONF)
        return mp


class KeystoneRuntime(KeystoneBase, RuntimeComponent):
    def __init__(self, *args, **kargs):
        KeystoneBase.__init__(self, *args, **kargs)


# Keystone setup commands are the the following
def _keystone_setup_cmds(components):

    # See http://keystone.openstack.org/man/keystone-manage.html

    root_cmd = ["%BIN_DIR%/keystone-manage", '--config-file=%CONFIG_FILE%']

    # Tenants
    tenant_cmds = [
        {
            "cmd": root_cmd + ["tenant", "add", "admin"],
        },
        {
            "cmd": root_cmd + ["tenant", "add", "demo"]
        },
        {
            "cmd": root_cmd + ["tenant", "add", "invisible_to_admin"]
        },
    ]

    # Users
    user_cmds = [
        {
            "cmd": root_cmd + ["user", "add", "admin", "%ADMIN_PASSWORD%"]
        },
        {
            "cmd": root_cmd + ["user", "add", "demo", "%ADMIN_PASSWORD%"]
        },
    ]

    # Roles
    role_cmds = [
        {
            "cmd": root_cmd + ["role", "add", "Admin"]
        },
        {
            "cmd": root_cmd + ["role", "add",  "Member"]
        },
        {
            "cmd": root_cmd + ["role", "add", "KeystoneAdmin"]
        },
        {
            "cmd": root_cmd + ["role", "add", "KeystoneServiceAdmin"]
        },
        {
            "cmd": root_cmd + ["role", "add", "sysadmin"]
        },
        {
            "cmd": root_cmd + ["role", "add", "netadmin"]
        },
        {
            "cmd": root_cmd + ["role", "grant", "Admin", "admin", "admin"]
        },
        {
            "cmd": root_cmd + ["role", "grant", "Member", "demo", "demo"]
        },
        {
            "cmd": root_cmd + ["role", "grant", "sysadmin", "demo", "demo"]
        },
        {
            "cmd": root_cmd + ["role", "grant", "netadmin", "demo", "demo"]
        },
        {
            "cmd": root_cmd + ["role", "grant", "Member", "demo", "invisible_to_admin"]
        },
        {
            "cmd": root_cmd + ["role", "grant", "Admin", "admin", "demo"]
        },
        {
            "cmd": root_cmd + ["role", "grant", "Admin", "admin"]
        },
        {
            "cmd": root_cmd + ["role", "grant", "KeystoneAdmin", "admin"]
        },
        {
            "cmd": root_cmd + ["role", "grant", "KeystoneServiceAdmin", "admin"]
        }
    ]

    # Services
    services = []
    services.append({
        "cmd": root_cmd + ["service", "add", "keystone", "identity", "Keystone Identity Service"]
    })

    if(NOVA in components):
        services.append({
                "cmd": root_cmd + ["service", "add", "nova", "compute", "Nova Compute Service"]
        })
        services.append({
                "cmd": root_cmd + ["service", "add", "ec2", "ec2", "EC2 Compatability Layer"]
        })

    if(GLANCE in components):
        services.append({
                "cmd": root_cmd + ["service", "add", "glance", "image", "Glance Image Service"]
        })

    if(SWIFT in components):
        services.append({
                "cmd": root_cmd + ["service", "add", "swift", "object-store", "Swift Service"]
        })

    # Endpoint templates
    endpoint_templates = list()
    endpoint_templates.append({
            "cmd": root_cmd + ["endpointTemplates", "add",
                "RegionOne", "keystone",
                "http://%HOST_IP%:5000/v2.0",
                "http://%HOST_IP%:35357/v2.0",
                "http://%HOST_IP%:5000/v2.0",
                "1",
                "1"
            ]
    })

    if(NOVA in components):
        endpoint_templates.append({
                "cmd": root_cmd + ["endpointTemplates", "add",
                    "RegionOne", "nova",
                    "http://%HOST_IP%:8774/v1.1/%tenant_id%",
                    "http://%HOST_IP%:8774/v1.1/%tenant_id%",
                    "http://%HOST_IP%:8774/v1.1/%tenant_id%",
                    "1",
                    "1"
            ]
        })
        endpoint_templates.append({
                "cmd": root_cmd + ["endpointTemplates", "add",
                    "RegionOne", "ec2",
                    "http://%HOST_IP%:8773/services/Cloud",
                    "http://%HOST_IP%:8773/services/Admin",
                    "http://%HOST_IP%:8773/services/Cloud",
                    "1",
                    "1"
            ]
        })

    if(GLANCE in components):
        endpoint_templates.append({
                "cmd": root_cmd + ["endpointTemplates", "add",
                    "RegionOne", "glance",
                    "http://%HOST_IP%:9292/v1.1/%tenant_id%",
                    "http://%HOST_IP%:9292/v1.1/%tenant_id%",
                    "http://%HOST_IP%:9292/v1.1/%tenant_id%",
                    "1",
                    "1"
            ]
        })

    if(SWIFT in components):
        endpoint_templates.append({
                "cmd": root_cmd + ["endpointTemplates", "add",
                    "RegionOne", "swift",
                    "http://%HOST_IP%:8080/v1/AUTH_%tenant_id%",
                    "http://%HOST_IP%:8080/",
                    "http://%HOST_IP%:8080/v1/AUTH_%tenant_id%",
                    "1",
                    "1"
            ]
        })

    # Tokens
    tokens = [
        {
            "cmd": root_cmd + ["token", "add", "%SERVICE_TOKEN%", "admin", "admin", "2015-02-05T00:00"]
        },
    ]

    # EC2 related creds - note we are setting the secret key to ADMIN_PASSWORD
    # but keystone doesn't parse them - it is just a blob from keystone's
    # point of view
    ec2_creds = []
    if(NOVA in components):
        ec2_creds = [
            {
                "cmd": root_cmd + ["credentials", "add",
                        "admin", "EC2", "admin", "%ADMIN_PASSWORD%", "admin" ]
            },
            {
                "cmd": root_cmd + ["credentials", "add",
                    "demo", "EC2", "demo", "%ADMIN_PASSWORD%", "demo"]
            }
        ]

    # Order matters here...
    all_cmds =  tenant_cmds + user_cmds + role_cmds + services + endpoint_templates + tokens + ec2_creds
    return all_cmds
