#!/usr/bin/python
#
# Copyright 2016 Canonical Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import shutil
import sys
import subprocess
import glob

try:
    import yaml  # flake8: noqa
except ImportError:
    if sys.version_info.major == 2:
        subprocess.check_call(['apt-get', 'install', '-y', 'python-yaml'])
    else:
        subprocess.check_call(['apt-get', 'install', '-y', 'python3-yaml'])
    import yaml  # flake8: noqa

import rabbit_utils as rabbit
import ssl_utils

from lib.utils import (
    chown, chmod,
    is_newer,
)
from charmhelpers.contrib.hahelpers.cluster import (
    is_clustered,
    is_elected_leader
)
from charmhelpers.contrib.openstack.utils import (
    is_unit_paused_set,
)

import charmhelpers.contrib.storage.linux.ceph as ceph
from charmhelpers.contrib.openstack.utils import save_script_rc
from charmhelpers.contrib.hardening.harden import harden

from charmhelpers.fetch import (
    add_source,
    apt_update,
    apt_install,
)

from charmhelpers.core.hookenv import (
    open_port,
    close_port,
    log,
    ERROR,
    INFO,
    relation_get,
    relation_clear,
    relation_set,
    relation_ids,
    related_units,
    service_name,
    local_unit,
    config,
    is_relation_made,
    Hooks,
    UnregisteredHookError,
    is_leader,
    charm_dir,
    status_set,
    unit_private_ip,
)
from charmhelpers.core.host import (
    cmp_pkgrevno,
    rsync,
    service_stop,
    service_restart,
    write_file,
)
from charmhelpers.contrib.charmsupport import nrpe

from charmhelpers.contrib.peerstorage import (
    peer_echo,
    peer_retrieve,
    peer_store,
    peer_store_and_set,
    peer_retrieve_by_prefix,
    leader_get,
)


hooks = Hooks()

SERVICE_NAME = os.getenv('JUJU_UNIT_NAME').split('/')[0]
POOL_NAME = SERVICE_NAME
RABBIT_DIR = '/var/lib/rabbitmq'
RABBIT_USER = 'rabbitmq'
RABBIT_GROUP = 'rabbitmq'
NAGIOS_PLUGINS = '/usr/local/lib/nagios/plugins'
SCRIPTS_DIR = '/usr/local/bin'
STATS_CRONFILE = '/etc/cron.d/rabbitmq-stats'
STATS_DATAFILE = os.path.join(RABBIT_DIR, 'data',
                              '{}_queue_stats.dat'
                              ''.format(rabbit.get_unit_hostname()))


@hooks.hook('install.real')
@harden()
def install():
    pre_install_hooks()
    # NOTE(jamespage) install actually happens in config_changed hook


def configure_amqp(username, vhost, admin=False):
    # get and update service password
    password = rabbit.get_rabbit_password(username)

    # update vhost
    rabbit.create_vhost(vhost)
    rabbit.create_user(username, password, admin)
    rabbit.grant_permissions(username, vhost)

    # NOTE(freyes): after rabbitmq-server 3.0 the method to define HA in the
    # queues is different
    # http://www.rabbitmq.com/blog/2012/11/19/breaking-things-with-rabbitmq-3-0
    if config('mirroring-queues'):
        rabbit.set_ha_mode(vhost, 'all')

    return password


def update_clients():
    """Update amqp client relation hooks

    IFF leader node is ready. Client nodes are considered ready once the leader
    has already run amqp_changed.
    """
    if rabbit.leader_node_is_ready() or rabbit.client_node_is_ready():
        for rid in relation_ids('amqp'):
            for unit in related_units(rid):
                amqp_changed(relation_id=rid, remote_unit=unit)


@hooks.hook('amqp-relation-changed')
def amqp_changed(relation_id=None, remote_unit=None):
    host_addr = rabbit.get_unit_ip()

    # TODO: Simplify what the non-leader needs to do
    if not is_leader() and rabbit.client_node_is_ready():
        # NOTE(jamespage) clear relation to deal with data being
        #                 removed from peer storage
        relation_clear(relation_id)
        # Each unit needs to set the db information otherwise if the unit
        # with the info dies the settings die with it Bug# 1355848
        exc_list = ['hostname', 'private-address']
        for rel_id in relation_ids('amqp'):
            peerdb_settings = peer_retrieve_by_prefix(rel_id,
                                                      exc_list=exc_list)
            peerdb_settings['hostname'] = host_addr
            peerdb_settings['private-address'] = host_addr
            if 'password' in peerdb_settings:
                relation_set(relation_id=rel_id, **peerdb_settings)

        log('amqp_changed(): Deferring amqp_changed'
            ' to the leader.')

        # NOTE: active/active case
        if config('prefer-ipv6'):
            relation_settings = {'private-address': host_addr}
            relation_set(relation_id=relation_id,
                         relation_settings=relation_settings)

        return

    # Bail if not completely ready
    if not rabbit.leader_node_is_ready():
        return

    relation_settings = {}
    settings = relation_get(rid=relation_id, unit=remote_unit)

    singleset = set(['username', 'vhost'])

    if singleset.issubset(settings):
        if None in [settings['username'], settings['vhost']]:
            log('amqp_changed(): Relation not ready.')
            return

        relation_settings['password'] = configure_amqp(
            username=settings['username'],
            vhost=settings['vhost'],
            admin=settings.get('admin', False))
    else:
        queues = {}
        for k, v in settings.iteritems():
            amqp = k.split('_')[0]
            x = '_'.join(k.split('_')[1:])
            if amqp not in queues:
                queues[amqp] = {}
            queues[amqp][x] = v
        for amqp in queues:
            if singleset.issubset(queues[amqp]):
                relation_settings[
                    '_'.join([amqp, 'password'])] = configure_amqp(
                    queues[amqp]['username'],
                    queues[amqp]['vhost'])

    relation_settings['hostname'] = \
        relation_settings['private-address'] = \
        rabbit.get_unit_ip()

    ssl_utils.configure_client_ssl(relation_settings)

    if is_clustered():
        relation_settings['clustered'] = 'true'
        if is_relation_made('ha'):
            # active/passive settings
            relation_settings['vip'] = config('vip')
            # or ha-vip-only to support active/active, but
            # accessed via a VIP for older clients.
            if config('ha-vip-only') is True:
                relation_settings['ha-vip-only'] = 'true'

    # set if need HA queues or not
    if cmp_pkgrevno('rabbitmq-server', '3.0.1') < 0:
        relation_settings['ha_queues'] = True
    peer_store_and_set(relation_id=relation_id,
                       relation_settings=relation_settings)


@hooks.hook('cluster-relation-joined')
def cluster_joined(relation_id=None):
    relation_settings = {
        'hostname': rabbit.get_unit_hostname(),
        'private-address':
            rabbit.get_unit_ip(config_override=rabbit.CLUSTER_OVERRIDE_CONFIG,
                               interface=rabbit.CLUSTER_INTERFACE),
    }

    relation_set(relation_id=relation_id,
                 relation_settings=relation_settings)

    if is_relation_made('ha') and \
            config('ha-vip-only') is False:
        log('hacluster relation is present, skipping native '
            'rabbitmq cluster config.')
        return

    try:
        if not is_leader():
            log('Not the leader, deferring cookie propagation to leader')
            return
    except NotImplementedError:
        if is_newer():
            log('cluster_joined: Relation greater.')
            return

    if not os.path.isfile(rabbit.COOKIE_PATH):
        log('erlang cookie missing from %s' % rabbit.COOKIE_PATH,
            level=ERROR)
        return

    if is_leader():
        log('Leader peer_storing cookie', level=INFO)
        cookie = open(rabbit.COOKIE_PATH, 'r').read().strip()
        peer_store('cookie', cookie)
        peer_store('leader_node_ip', unit_private_ip())
        peer_store('leader_node_hostname', rabbit.get_unit_hostname())


@hooks.hook('cluster-relation-changed')
def cluster_changed(relation_id=None, remote_unit=None):
    # Future travelers beware ordering is significant
    rdata = relation_get(rid=relation_id, unit=remote_unit)

    # sync passwords
    blacklist = ['hostname', 'private-address', 'public-address']
    whitelist = [a for a in rdata.keys() if a not in blacklist]
    peer_echo(includes=whitelist)

    cookie = peer_retrieve('cookie')
    if not cookie:
        log('cluster_changed: cookie not yet set.', level=INFO)
        return

    if rdata:
        hostname = rdata.get('hostname', None)
        private_address = rdata.get('private-address', None)

        if hostname and private_address:
            rabbit.update_hosts_file({private_address: hostname})

    # sync the cookie with peers if necessary
    update_cookie()

    if is_relation_made('ha') and \
            config('ha-vip-only') is False:
        log('hacluster relation is present, skipping native '
            'rabbitmq cluster config.', level=INFO)
        return

    # cluster with node?
    if not is_leader():
        rabbit.cluster_with()
        update_nrpe_checks()


def update_cookie(leaders_cookie=None):
    # sync cookie
    if leaders_cookie:
        cookie = leaders_cookie
    else:
        cookie = peer_retrieve('cookie')
    cookie_local = None
    with open(rabbit.COOKIE_PATH, 'r') as f:
        cookie_local = f.read().strip()

    if cookie_local == cookie:
        log('Cookie already synchronized with peer.')
        return

    service_stop('rabbitmq-server')
    with open(rabbit.COOKIE_PATH, 'wb') as out:
        out.write(cookie)
    if not is_unit_paused_set():
        service_restart('rabbitmq-server')
        rabbit.wait_app()


@hooks.hook('ha-relation-joined')
def ha_joined():
    corosync_bindiface = config('ha-bindiface')
    corosync_mcastport = config('ha-mcastport')
    vip = config('vip')
    vip_iface = config('vip_iface')
    vip_cidr = config('vip_cidr')
    rbd_name = config('rbd-name')
    vip_only = config('ha-vip-only')

    if None in [corosync_bindiface, corosync_mcastport, vip, vip_iface,
                vip_cidr, rbd_name] and vip_only is False:
        log('Insufficient configuration data to configure hacluster.',
            level=ERROR)
        sys.exit(1)
    elif None in [corosync_bindiface, corosync_mcastport, vip, vip_iface,
                  vip_cidr] and vip_only is True:
        log('Insufficient configuration data to configure VIP-only hacluster.',
            level=ERROR)
        sys.exit(1)

    if not is_relation_made('ceph', 'auth') and vip_only is False:
        log('ha_joined: No ceph relation yet, deferring.')
        return

    name = '%s@localhost' % SERVICE_NAME
    if rabbit.get_node_name() != name and vip_only is False:
        log('Stopping rabbitmq-server.')
        service_stop('rabbitmq-server')
        rabbit.update_rmq_env_conf(hostname='%s@localhost' % SERVICE_NAME,
                                   ipv6=config('prefer-ipv6'))
    else:
        log('Node name already set to %s.' % name)

    relation_settings = {}
    relation_settings['corosync_bindiface'] = corosync_bindiface
    relation_settings['corosync_mcastport'] = corosync_mcastport

    if vip_only is True:
        relation_settings['resources'] = {
            'res_rabbitmq_vip': 'ocf:heartbeat:IPaddr2',
        }
        relation_settings['resource_params'] = {
            'res_rabbitmq_vip': 'params ip="%s" cidr_netmask="%s" nic="%s"' %
                                (vip, vip_cidr, vip_iface),
        }
    else:
        relation_settings['resources'] = {
            'res_rabbitmq_rbd': 'ocf:ceph:rbd',
            'res_rabbitmq_fs': 'ocf:heartbeat:Filesystem',
            'res_rabbitmq_vip': 'ocf:heartbeat:IPaddr2',
            'res_rabbitmq-server': 'lsb:rabbitmq-server',
        }

        relation_settings['resource_params'] = {
            'res_rabbitmq_rbd': 'params name="%s" pool="%s" user="%s" '
                                'secret="%s"' %
                                (rbd_name, POOL_NAME,
                                 SERVICE_NAME, ceph._keyfile_path(
                                     SERVICE_NAME)),
            'res_rabbitmq_fs': 'params device="/dev/rbd/%s/%s" directory="%s" '
                               'fstype="ext4" op start start-delay="10s"' %
                               (POOL_NAME, rbd_name, RABBIT_DIR),
            'res_rabbitmq_vip': 'params ip="%s" cidr_netmask="%s" nic="%s"' %
                                (vip, vip_cidr, vip_iface),
            'res_rabbitmq-server': 'op start start-delay="5s" '
                                   'op monitor interval="5s"',
        }

        relation_settings['groups'] = {
            'grp_rabbitmq':
            'res_rabbitmq_rbd res_rabbitmq_fs res_rabbitmq_vip '
            'res_rabbitmq-server',
        }

    for rel_id in relation_ids('ha'):
        relation_set(relation_id=rel_id, relation_settings=relation_settings)

    env_vars = {
        'OPENSTACK_PORT_EPMD': 4369,
        'OPENSTACK_PORT_MCASTPORT': config('ha-mcastport'),
    }
    save_script_rc(**env_vars)


@hooks.hook('ha-relation-changed')
def ha_changed():
    if not is_clustered():
        return
    vip = config('vip')
    log('ha_changed(): We are now HA clustered. '
        'Advertising our VIP (%s) to all AMQP clients.' %
        vip)


@hooks.hook('ceph-relation-joined')
def ceph_joined():
    log('Start Ceph Relation Joined')
    # NOTE fixup
    # utils.configure_source()
    ceph.install()
    log('Finish Ceph Relation Joined')


@hooks.hook('ceph-relation-changed')
def ceph_changed():
    log('Start Ceph Relation Changed')
    auth = relation_get('auth')
    key = relation_get('key')
    use_syslog = str(config('use-syslog')).lower()
    if None in [auth, key]:
        log('Missing key or auth in relation')
        sys.exit(0)

    ceph.configure(service=SERVICE_NAME, key=key, auth=auth,
                   use_syslog=use_syslog)

    if is_elected_leader('res_rabbitmq_vip'):
        rbd_img = config('rbd-name')
        rbd_size = config('rbd-size')
        sizemb = int(rbd_size.split('G')[0]) * 1024
        blk_device = '/dev/rbd/%s/%s' % (POOL_NAME, rbd_img)
        ceph.create_pool(service=SERVICE_NAME, name=POOL_NAME,
                         replicas=int(config('ceph-osd-replication-count')))
        ceph.ensure_ceph_storage(service=SERVICE_NAME, pool=POOL_NAME,
                                 rbd_img=rbd_img, sizemb=sizemb,
                                 fstype='ext4', mount_point=RABBIT_DIR,
                                 blk_device=blk_device,
                                 system_services=['rabbitmq-server'])
        subprocess.check_call(['chown', '-R', '%s:%s' %
                               (RABBIT_USER, RABBIT_GROUP), RABBIT_DIR])
    else:
        log('This is not the peer leader. Not configuring RBD.')
        log('Stopping rabbitmq-server.')
        service_stop('rabbitmq-server')

    # If 'ha' relation has been made before the 'ceph' relation
    # it is important to make sure the ha-relation data is being
    # sent.
    if is_relation_made('ha'):
        log('*ha* relation exists. Triggering ha_joined()')
        ha_joined()
    else:
        log('*ha* relation does not exist.')
    log('Finish Ceph Relation Changed')


@hooks.hook('nrpe-external-master-relation-changed')
def update_nrpe_checks():
    if os.path.isdir(NAGIOS_PLUGINS):
        rsync(os.path.join(os.getenv('CHARM_DIR'), 'scripts',
                           'check_rabbitmq.py'),
              os.path.join(NAGIOS_PLUGINS, 'check_rabbitmq.py'))
        rsync(os.path.join(os.getenv('CHARM_DIR'), 'scripts',
                           'check_rabbitmq_queues.py'),
              os.path.join(NAGIOS_PLUGINS, 'check_rabbitmq_queues.py'))
    if config('stats_cron_schedule'):
        script = os.path.join(SCRIPTS_DIR, 'collect_rabbitmq_stats.sh')
        cronjob = "{} root {}\n".format(config('stats_cron_schedule'), script)
        rsync(os.path.join(charm_dir(), 'scripts',
                           'collect_rabbitmq_stats.sh'), script)
        write_file(STATS_CRONFILE, cronjob)
    elif os.path.isfile(STATS_CRONFILE):
        os.remove(STATS_CRONFILE)

    # Find out if nrpe set nagios_hostname
    hostname = nrpe.get_nagios_hostname()
    myunit = nrpe.get_nagios_unit_name()

    # create unique user and vhost for each unit
    current_unit = local_unit().replace('/', '-')
    user = 'nagios-%s' % current_unit
    vhost = 'nagios-%s' % current_unit
    password = rabbit.get_rabbit_password(user, local=True)

    rabbit.create_vhost(vhost)
    rabbit.create_user(user, password)
    rabbit.grant_permissions(user, vhost)

    nrpe_compat = nrpe.NRPE(hostname=hostname)
    nrpe_compat.add_check(
        shortname=rabbit.RABBIT_USER,
        description='Check RabbitMQ {%s}' % myunit,
        check_cmd='{}/check_rabbitmq.py --user {} --password {} --vhost {}'
                  ''.format(NAGIOS_PLUGINS, user, password, vhost)
    )
    if config('queue_thresholds'):
        cmd = ""
        # If value of queue_thresholds is incorrect we want the hook to fail
        for item in yaml.safe_load(config('queue_thresholds')):
            cmd += ' -c "{}" "{}" {} {}'.format(*item)
        nrpe_compat.add_check(
            shortname=rabbit.RABBIT_USER + '_queue',
            description='Check RabbitMQ Queues',
            check_cmd='{}/check_rabbitmq_queues.py{} {}'.format(
                        NAGIOS_PLUGINS, cmd, STATS_DATAFILE)
        )
    nrpe_compat.write()


@hooks.hook('upgrade-charm')
@harden()
def upgrade_charm():
    pre_install_hooks()
    add_source(config('source'), config('key'))
    apt_update(fatal=True)

    # Ensure older passwd files in /var/lib/juju are moved to
    # /var/lib/rabbitmq which will end up replicated if clustered
    for f in [f for f in os.listdir('/var/lib/juju')
              if os.path.isfile(os.path.join('/var/lib/juju', f))]:
        if f.endswith('.passwd'):
            s = os.path.join('/var/lib/juju', f)
            d = os.path.join('/var/lib/charm/{}'.format(service_name()), f)

            log('upgrade_charm: Migrating stored passwd'
                ' from %s to %s.' % (s, d))
            shutil.move(s, d)
    if is_elected_leader('res_rabbitmq_vip'):
        rabbit.migrate_passwords_to_peer_relation()

    # explicitly update buggy file name naigos.passwd
    old = os.path.join('var/lib/rabbitmq', 'naigos.passwd')
    if os.path.isfile(old):
        new = os.path.join('var/lib/rabbitmq', 'nagios.passwd')
        shutil.move(old, new)


MAN_PLUGIN = 'rabbitmq_management'


@hooks.hook('config-changed')
@rabbit.restart_on_change(rabbit.restart_map())
@harden()
def config_changed():

    # Update hosts with this unit's information
    rabbit.update_hosts_file(
        {rabbit.get_unit_ip(config_override=rabbit.CLUSTER_OVERRIDE_CONFIG,
                            interface=rabbit.CLUSTER_INTERFACE):
                                rabbit.get_unit_hostname()})

    # Add archive source if provided
    add_source(config('source'), config('key'))
    apt_update(fatal=True)
    # Copy in defaults file for updated ulimits
    shutil.copyfile(
        'templates/rabbitmq-server',
        '/etc/default/rabbitmq-server')
    # Install packages to ensure any changes to source
    # result in an upgrade if applicable.
    status_set('maintenance', 'Installing/upgrading RabbitMQ packages')
    apt_install(rabbit.PACKAGES, fatal=True)

    open_port(5672)

    chown(RABBIT_DIR, rabbit.RABBIT_USER, rabbit.RABBIT_USER)
    chmod(RABBIT_DIR, 0o775)

    if config('management_plugin') is True:
        rabbit.enable_plugin(MAN_PLUGIN)
        open_port(rabbit.get_managment_port())
    else:
        rabbit.disable_plugin(MAN_PLUGIN)
        close_port(rabbit.get_managment_port())
        # LY: Close the old managment port since it may have been opened in a
        #     previous version of the charm. close_port is a noop if the port
        #     is not open
        close_port(55672)

    rabbit.ConfigRenderer(
        rabbit.CONFIG_FILES).write_all()

    # Only set values if this is the leader
    if not is_leader():
        return

    rabbit.set_all_mirroring_queues(config('mirroring-queues'))

    if is_relation_made("ha"):
        ha_is_active_active = config("ha-vip-only")

        if ha_is_active_active:
            update_nrpe_checks()
        else:
            if is_elected_leader('res_rabbitmq_vip'):
                update_nrpe_checks()
            else:
                log("hacluster relation is present but this node is not active"
                    " skipping update nrpe checks")
    else:
        update_nrpe_checks()

    # Update cluster in case min-cluster-size has changed
    for rid in relation_ids('cluster'):
        for unit in related_units(rid):
            cluster_changed(relation_id=rid, remote_unit=unit)


@hooks.hook('leader-elected')
def leader_elected():
    status_set("maintenance", "{} is the elected leader".format(local_unit()))


@hooks.hook('leader-settings-changed')
def leader_settings_changed():
    if not os.path.exists(rabbit.RABBITMQ_CTL):
        log('Deferring cookie configuration, RabbitMQ not yet installed')
        return
    # Get cookie from leader, update cookie locally and
    # force cluster-relation-changed hooks to run on peers
    cookie = leader_get(attribute='cookie')
    if cookie:
        update_cookie(leaders_cookie=cookie)
        # Force cluster-relation-changed hooks to run on peers
        # This will precipitate peer clustering
        # Without this a chicken and egg scenario prevails when
        # using LE and peerstorage
        for rid in relation_ids('cluster'):
            relation_set(relation_id=rid, relation_settings={'cookie': cookie})


def pre_install_hooks():
    for f in glob.glob('exec.d/*/charm-pre-install'):
        if os.path.isfile(f) and os.access(f, os.X_OK):
            subprocess.check_call(['sh', '-c', f])


@hooks.hook('update-status')
@harden()
def update_status():
    log('Updating status.')


if __name__ == '__main__':
    try:
        hooks.execute(sys.argv)
    except UnregisteredHookError as e:
        log('Unknown hook {} - skipping.'.format(e))
    # Gated client updates
    update_clients()
    rabbit.assess_status(rabbit.ConfigRenderer(rabbit.CONFIG_FILES))
