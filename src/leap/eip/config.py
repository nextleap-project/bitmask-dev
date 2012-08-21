import ConfigParser
import grp
import logging
import os
import json
import platform
import socket

from leap.util.fileutil import (which, mkdir_p,
                                check_and_fix_urw_only)
from leap.baseapp.permcheck import (is_pkexec_in_system,
                                    is_auth_agent_running)

logger = logging.getLogger(name=__name__)
logger.setLevel('DEBUG')

OPENVPN_CONFIG_TEMPLATE = """#Autogenerated by eip-client wizard
remote {VPN_REMOTE_HOST} {VPN_REMOTE_PORT}

client
dev tun
persist-tun
persist-key
proto udp
tls-client
remote-cert-tls server

cert {LEAP_EIP_KEYS}
key {LEAP_EIP_KEYS}
ca {LEAP_EIP_KEYS}
"""


def get_config_dir():
    """
    get the base dir for all leap config
    @rparam: config path
    @rtype: string
    """
    # TODO
    # check for $XDG_CONFIG_HOME var?
    # get a more sensible path for win/mac
    # kclair: opinion? ^^
    return os.path.expanduser(
        os.path.join('~',
                     '.config',
                     'leap'))


def get_config_file(filename, folder=None):
    """
    concatenates the given filename
    with leap config dir.
    @param filename: name of the file
    @type filename: string
    @rparam: full path to config file
    """
    path = []
    path.append(get_config_dir())
    if folder is not None:
        path.append(folder)
    path.append(filename)
    return os.path.join(*path)


def get_default_provider_path():
    default_subpath = os.path.join("providers",
                                   "default")
    default_provider_path = get_config_file(
        '',
        folder=default_subpath)
    return default_provider_path


def validate_ip(ip_str):
    """
    raises exception if the ip_str is
    not a valid representation of an ip
    """
    socket.inet_aton(ip_str)


def check_or_create_default_vpnconf(config):
    """
    checks that a vpn config file
    exists for a default provider,
    or creates one if it does not.
    ATM REQURES A [provider] section in
    eip.cfg with _at least_ a remote_ip value
    """
    default_provider_path = get_default_provider_path()

    if not os.path.isdir(default_provider_path):
        mkdir_p(default_provider_path)

    conf_file = get_config_file(
        'openvpn.conf',
        folder=default_provider_path)

    if os.path.isfile(conf_file):
        return
    else:
        logger.debug(
            'missing default openvpn config\n'
            'creating one...')

    # We're getting provider from eip.cfg
    # by now. Get it from a list of gateways
    # instead.

    try:
        # XXX by now, we're expecting
        # only IP format for remote.
        # We should allow also domain names,
        # and make a reverse resolv.
        remote_ip = config.get('provider',
                               'remote_ip')
        validate_ip(remote_ip)

    except ConfigParser.NoSectionError:
        raise eip_exceptions.EIPInitNoProviderError

    except socket.error:
        # this does not look like an ip, dave
        raise EIPInitBadProviderError

    if config.has_option('provider', 'remote_port'):
        remote_port = config.get('provider',
                                 'remote_port')
    else:
        remote_port = 1194

    default_subpath = os.path.join("providers",
                                   "default")
    default_provider_path = get_config_file(
        '',
        folder=default_subpath)

    if not os.path.isdir(default_provider_path):
        mkdir_p(default_provider_path)

    conf_file = get_config_file(
        'openvpn.conf',
        folder=default_provider_path)

    # XXX keys have to be manually placed by now
    keys_file = get_config_file(
        'openvpn.keys',
        folder=default_provider_path)

    ovpn_config = OPENVPN_CONFIG_TEMPLATE.format(
        VPN_REMOTE_HOST=remote_ip,
        VPN_REMOTE_PORT=remote_port,
        LEAP_EIP_KEYS=keys_file)

    with open(conf_file, 'wb') as f:
        f.write(ovpn_config)


def get_username():
    return os.getlogin()


def get_groupname():
    gid = os.getgroups()[-1]
    return grp.getgrgid(gid).gr_name


def build_ovpn_options(daemon=False):
    """
    build a list of options
    to be passed in the
    openvpn invocation
    @rtype: list
    @rparam: options
    """
    # XXX review which of the
    # options we don't need.

    # TODO pass also the config file,
    # since we will need to take some
    # things from there if present.

    # get user/group name
    # also from config.
    user = get_username()
    group = get_groupname()

    opts = []

    # set user and group
    opts.append('--user')
    opts.append('%s' % user)
    opts.append('--group')
    opts.append('%s' % group)

    opts.append('--management-client-user')
    opts.append('%s' % user)
    opts.append('--management-signal')

    # set default options for management
    # interface. unix sockets or telnet interface for win.
    # XXX take them from the config object.

    ourplatform = platform.system()
    if ourplatform in ("Linux", "Mac"):
        opts.append('--management')
        opts.append('/tmp/.eip.sock')
        opts.append('unix')
    if ourplatform == "Windows":
        opts.append('--management')
        opts.append('localhost')
        # XXX which is a good choice?
        opts.append('7777')

    # remaining config options will go in a file

    # NOTE: we will build this file from
    # the service definition file.
    # XXX override from --with-openvpn-config

    opts.append('--config')

    default_provider_path = get_default_provider_path()

    # XXX get rid of config_file at all
    ovpncnf = get_config_file(
        'openvpn.conf',
        folder=default_provider_path)
    opts.append(ovpncnf)

    # we cannot run in daemon mode
    # with the current subp setting.
    # see: https://leap.se/code/issues/383
    #if daemon is True:
        #opts.append('--daemon')

    return opts


def build_ovpn_command(config, debug=False, do_pkexec_check=True):
    """
    build a string with the
    complete openvpn invocation

    @param config: config object
    @type config: ConfigParser instance

    @rtype [string, [list of strings]]
    @rparam: a list containing the command string
        and a list of options.
    """
    command = []
    use_pkexec = True
    ovpn = None

    if config.has_option('openvpn', 'use_pkexec'):
        use_pkexec = config.get('openvpn', 'use_pkexec')
    if platform.system() == "Linux" and use_pkexec and do_pkexec_check:

        # XXX check for both pkexec (done)
        # AND a suitable authentication
        # agent running.
        logger.info('use_pkexec set to True')

        if not is_pkexec_in_system():
            logger.error('no pkexec in system')
            raise EIPNoPkexecAvailable

        if not is_auth_agent_running():
            logger.warning(
                "no polkit auth agent found. "
                "pkexec will use its own text "
                "based authentication agent. "
                "that's probably a bad idea")
            raise EIPNoPolkitAuthAgentAvailable

        command.append('pkexec')

    if config.has_option('openvpn',
                         'openvpn_binary'):
        ovpn = config.get('openvpn',
                          'openvpn_binary')
    if not ovpn and config.has_option('DEFAULT',
                                      'openvpn_binary'):
        ovpn = config.get('DEFAULT',
                          'openvpn_binary')

    if ovpn:
        vpn_command = ovpn
    else:
        vpn_command = "openvpn"

    command.append(vpn_command)

    daemon_mode = not debug

    for opt in build_ovpn_options(daemon=daemon_mode):
        command.append(opt)

    # XXX check len and raise proper error

    return [command[0], command[1:]]


def get_sensible_defaults():
    """
    gathers a dict of sensible defaults,
    platform sensitive,
    to be used to initialize the config parser
    @rtype: dict
    @rparam: default options.
    """

    # this way we're passing a simple dict
    # that will initialize the configparser
    # and will get written to "DEFAULTS" section,
    # which is fine for now.
    # if we want to write to a particular section
    # we can better pass a tuple of triples
    # (('section1', 'foo', '23'),)
    # and config.set them

    defaults = dict()
    defaults['openvpn_binary'] = which('openvpn')
    defaults['autostart'] = 'true'

    # TODO
    # - management.
    return defaults


def get_config(config_file=None):
    """
    temporary method for getting configs,
    mainly for early stage development process.
    in the future we will get preferences
    from the storage api

    @rtype: ConfigParser instance
    @rparam: a config object
    """
    # TODO
    # - refactor out common things and get
    # them to util/ or baseapp/

    defaults = get_sensible_defaults()
    config = ConfigParser.ConfigParser(defaults)

    if not config_file:
        fpath = get_config_file('eip.cfg')
        if not os.path.isfile(fpath):
            dpath, cfile = os.path.split(fpath)
            if not os.path.isdir(dpath):
                mkdir_p(dpath)
            with open(fpath, 'wb') as configfile:
                config.write(configfile)
        config_file = open(fpath)

    #TODO
    # - convert config_file to list;
    #   look in places like /etc/leap/eip.cfg
    #   for global settings.
    # - raise warnings/error if bad options.

    # at this point, the file should exist.
    # errors would have been raised above.

    config.readfp(config_file)

    return config


def check_vpn_keys(config):
    """
    performs an existance and permission check
    over the openvpn keys file.
    Currently we're expecting a single file
    per provider, containing the CA cert,
    the provider key, and our client certificate
    """

    keyopt = ('provider', 'keyfile')

    # XXX at some point,
    # should separate between CA, provider cert
    # and our certificate.
    # make changes in the default provider template
    # accordingly.

    # get vpn keys
    if config.has_option(*keyopt):
        keyfile = config.get(*keyopt)
    else:
        keyfile = get_config_file(
            'openvpn.keys',
            folder=get_default_provider_path())
        logger.debug('keyfile = %s', keyfile)

    # if no keys, raise error.
    # should be catched by the ui and signal user.

    if not os.path.isfile(keyfile):
        logger.error('key file %s not found. aborting.',
                     keyfile)
        raise eip_exceptions.EIPInitNoKeyFileError

    # check proper permission on keys
    # bad perms? try to fix them
    try:
        check_and_fix_urw_only(keyfile)
    except OSError:
        raise EIPInitBadKeyFilePermError


def get_config_json(config_file=None):
    """
    will replace get_config function be developing them
    in parralel for branch purposes.
    @param: configuration file
    @type: file
    @rparam: configuration turples
    @rtype: dictionary
    """
    if not config_file:
        fpath = get_config_file('eip.json')
        if not os.path.isfile(fpath):
            dpath, cfile = os.path.split(fpath)
            if not os.path.isdir(dpath):
                mkdir_p(dpath)
            with open(fpath, 'wb') as configfile:
                configfile.flush()
        config_file = open(fpath)

    config = json.load(config_file)

    return config
