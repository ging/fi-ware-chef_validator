# -*- coding: utf-8 -*-
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

import collections
import email
from email.mime import multipart
from email.mime import text
import logging
import os
import pkgutil
import string
import urlparse

from novaclient import client as nc
from novaclient import exceptions
from oslo_config import cfg
from oslo_serialization import jsonutils
from oslo_utils import uuidutils
import six

from chef_validator.common import exception
from chef_validator.common.i18n import _
from chef_validator.common.i18n import _LW
from chef_validator.engine.clients import client_plugin

LOG = logging.getLogger(__name__)

NOVACLIENT_VERSION = "2"
# todo resource usage check
resource = None


class NovaClientPlugin(client_plugin.ClientPlugin):
    deferred_server_statuses = ['BUILD',
                                'HARD_REBOOT',
                                'PASSWORD',
                                'REBOOT',
                                'RESCUE',
                                'RESIZE',
                                'REVERT_RESIZE',
                                'SHUTOFF',
                                'SUSPENDED',
                                'VERIFY_RESIZE']

    exceptions_module = exceptions
    service_types = ['compute']

    def _create(self):
        endpoint_type = self._get_client_option('nova', 'endpoint_type')
        management_url = self.url_for(service_type=self.service_types[0],
                                      endpoint_type=endpoint_type)

        if hasattr(nc, 'discover_extensions'):
            extensions = nc.discover_extensions(NOVACLIENT_VERSION)
        else:
            # TODO(lyj): The else condition is for backward compatibility,
            #            once novaclient bump to a newer version with
            #            discover_extensions exists, this should be safely
            #            removed.
            from novaclient import shell as novashell
            computeshell = novashell.OpenStackComputeShell()
            extensions = computeshell._discover_extensions(NOVACLIENT_VERSION)

        args = {
            'project_id': self.context.tenant,
            'auth_url': self.context.auth_url,
            'service_type': self.service_types[0],
            'username': None,
            'api_key': None,
            'extensions': extensions,
            'endpoint_type': endpoint_type,
            'http_log_debug': self._get_client_option('nova',
                                                      'http_log_debug'),
            'cacert': self._get_client_option('nova', 'ca_file'),
            'insecure': self._get_client_option('nova', 'insecure')
        }

        client = nc.Client(NOVACLIENT_VERSION, **args)
        client.client.auth_token = self.auth_token
        client.client.management_url = management_url

        return client

    def is_not_found(self, ex):
        return isinstance(ex, exceptions.NotFound)

    def is_over_limit(self, ex):
        return isinstance(ex, exceptions.OverLimit)

    def is_bad_request(self, ex):
        return isinstance(ex, exceptions.BadRequest)

    def is_conflict(self, ex):
        return isinstance(ex, exceptions.Conflict)

    def is_unprocessable_entity(self, ex):
        http_status = (getattr(ex, 'http_status', None) or
                       getattr(ex, 'code', None))
        return (isinstance(ex, exceptions.ClientException) and
                http_status == 422)

    def get_server(self, server):
        """Return fresh server object.

        Substitutes Nova's NotFound for chef_validator's EntityNotFound,
        to be returned to user as HTTP error.
        """
        try:
            return self.client().servers.get(server)
        except exceptions.NotFound as ex:
            LOG.warn(_LW('Server (%(server)s) not found: %(ex)s'),
                     {'server': server, 'ex': ex})
            raise exception.EntityNotFound(entity='Server', name=server)

    def fetch_server(self, server_id):
        """
        Fetch fresh server object from Nova.

        Log warnings and return None for non-critical API errors.
        Use this method in various ``check_*_complete`` resource methods,
        where intermittent errors can be tolerated.
        """
        server = None
        try:
            server = self.client().servers.get(server_id)
        except exceptions.OverLimit as exc:
            LOG.warn(_LW("Received an OverLimit response when fetching server "
                         "(%(id)s) : %(exception)s"),
                     {'id': server_id, 'exception': exc})
        except exceptions.ClientException as exc:
            if getattr(exc, 'http_status', getattr(exc, 'code', None)) \
                    in (500, 503):
                LOG.warn(_LW("Received the following exception "
                             "when fetching server (%(id)s) : %(exception)s"),
                         {'id': server_id, 'exception': exc})
            else:
                raise
        return server

    def refresh_server(self, server):
        """
        Refresh server's attributes and log warnings for non-critical
        API errors.
        """
        try:
            server.get()
        except exceptions.OverLimit as exc:
            LOG.warn(_LW("Server %(name)s (%(id)s) received an OverLimit "
                         "response during server.get(): %(exception)s"),
                     {'name': server.name, 'id': server.id, 'exception': exc})
        except exceptions.ClientException as exc:
            if ((getattr(exc, 'http_status', getattr(exc, 'code', None))
                 in (500, 503))):
                LOG.warn(_LW('Server "%(name)s" (%(id)s) received the '
                             'following exception during server.get(): '
                             '%(exception)s'),
                         {'name': server.name, 'id': server.id,
                          'exception': exc})
            else:
                raise

    def get_ip(self, server, net_type, ip_version):
        """Return the server's IP of the given type and version."""
        if net_type in server.addresses:
            for ip in server.addresses[net_type]:
                if ip['version'] == ip_version:
                    return ip['addr']

    def get_status(self, server):
        """
        Return the server's status.
        :param server: server object
        :returns: status as a string
        """
        # Some clouds append extra (STATUS) strings to the status, strip it
        return server.status.split('(')[0]

    def _check_active(self, server, res_name='Server'):
        """Check server status.

        Accepts both server IDs and server objects.
        Returns True if server is ACTIVE, raises errors when server has an
        ERROR or unknown to chef_validator status,
        returns False otherwise.

        :param res_name: name of the resource to use in the exception message

        """
        # not checking with is_uuid_like as most tests use strings e.g. '1234'
        if isinstance(server, six.string_types):
            server = self.fetch_server(server)
            if server is None:
                return False
            else:
                status = self.get_status(server)
        else:
            status = self.get_status(server)
            if status != 'ACTIVE':
                self.refresh_server(server)
                status = self.get_status(server)

        if status in self.deferred_server_statuses:
            return False
        elif status == 'ACTIVE':
            return True
        elif status == 'ERROR':
            fault = getattr(server, 'fault', {})
            raise resource.ResourceInError(
                resource_status=status,
                status_reason=_("Message: %(message)s, Code: %(code)s") %
                {'message': fault.get('message', _('Unknown')),
                 'code': fault.get('code', _('Unknown'))})
        else:
            raise resource.ResourceUnknownStatus(
                resource_status=server.status,
                result=_('%s is not active') % res_name)

    def get_flavor_id(self, flavor):
        """Get the id for the specified flavor name.
        If the specified value is flavor id, just return it.

        :param flavor: the name of the flavor to find
        :returns: the id of :flavor:
        :raises: exception.FlavorMissing
        """
        flavor_id = None
        flavor_list = self.client().flavors.list()
        for o in flavor_list:
            if o.name == flavor:
                flavor_id = o.id
                break
            if o.id == flavor:
                flavor_id = o.id
                break
        if flavor_id is None:
            raise exception.FlavorMissing(flavor_id=flavor)
        return flavor_id

    def get_keypair(self, key_name):
        """Get the public key specified by :key_name:

        :param key_name: the name of the key to look for
        :returns: the keypair (name, public_key) for :key_name:
        :raises: exception.UserKeyPairMissing
        """
        try:
            return self.client().keypairs.get(key_name)
        except exceptions.NotFound:
            raise exception.UserKeyPairMissing(key_name=key_name)

    def build_userdata(self, metadata, userdata=None, instance_user=None,
                       user_data_format='chef_validator_CFNTOOLS'):
        """Build multipart data blob for CloudInit which includes user-supplied
        Metadata, user data, and the required chef_validator in-instance
        configuration.

        :param resource: the resource implementation
        :type resource: chef_validator.engine.Resource
        :param userdata: user data string
        :type userdata: str or None
        :param instance_user: the user to create on the server
        :type instance_user: string
        :param user_data_format: Format of user data to return
        :type user_data_format: string
        :returns: multipart mime as a string
        """

        if user_data_format == 'RAW':
            return userdata

        is_cfntools = user_data_format == 'chef_validator_CFNTOOLS'
        is_software_config = user_data_format == 'SOFTWARE_CONFIG'

        def make_subpart(content, filename, subtype=None):
            if subtype is None:
                subtype = os.path.splitext(filename)[0]
            msg = text.MIMEText(content, _subtype=subtype)
            msg.add_header('Content-Disposition', 'attachment',
                           filename=filename)
            return msg

        def read_cloudinit_file(fn):
            return pkgutil.get_data('chef_validator', 'cloudinit/%s' % fn)

        if instance_user:
            config_custom_user = 'user: %s' % instance_user
            # FIXME(shadower): compatibility workaround for cloud-init 0.6.3.
            # We can drop this once we stop supporting 0.6.3 (which ships
            # with Ubuntu 12.04 LTS).
            #
            # See bug https://bugs.launchpad.net/chef_validator/+bug/1257410
            boothook_custom_user = r"""useradd -m %s
echo -e '%s\tALL=(ALL)\tNOPASSWD: ALL' >> /etc/sudoers
""" % (instance_user, instance_user)
        else:
            config_custom_user = ''
            boothook_custom_user = ''

        cloudinit_config = string.Template(
            read_cloudinit_file('config')).safe_substitute(
            add_custom_user=config_custom_user)
        cloudinit_boothook = string.Template(
            read_cloudinit_file('boothook.sh')).safe_substitute(
            add_custom_user=boothook_custom_user)
        attachments = [(cloudinit_config, 'cloud-config'),
                       (cloudinit_boothook, 'boothook.sh', 'cloud-boothook'),
                       (read_cloudinit_file('part_handler.py'),
                        'part-handler.py')]

        if is_cfntools:
            attachments.append((userdata, 'cfn-userdata', 'x-cfninitdata'))
        elif is_software_config:
            # attempt to parse userdata as a multipart message, and if it
            # is, add each part as an attachment
            userdata_parts = None
            try:
                userdata_parts = email.message_from_string(userdata)
            except Exception:
                pass
            if userdata_parts and userdata_parts.is_multipart():
                for part in userdata_parts.get_payload():
                    attachments.append((part.get_payload(),
                                        part.get_filename(),
                                        part.get_content_subtype()))
            else:
                attachments.append((userdata, 'userdata', 'x-shellscript'))

        if is_cfntools:
            attachments.append((read_cloudinit_file('loguserdata.py'),
                                'loguserdata.py', 'x-shellscript'))

        if metadata:
            attachments.append((jsonutils.dumps(metadata), 'cfn-init-data',
                                'x-cfninitdata'))

        attachments.append((cfg.CONF.chef_validator_watch_server_url,
                            'cfn-watch-server', 'x-cfninitdata'))

        if is_cfntools:
            attachments.append((cfg.CONF.chef_validator_metadata_server_url,
                                'cfn-metadata-server', 'x-cfninitdata'))

            # Create a boto config which the cfntools on the host use to know
            # where the cfn and cw API's are to be accessed
            cfn_url = urlparse.urlparse(
                cfg.CONF.chef_validator_metadata_server_url)
            cw_url = urlparse.urlparse(
                cfg.CONF.chef_validator_watch_server_url)
            is_secure = cfg.CONF.instance_connection_is_secure
            vcerts = cfg.CONF.instance_connection_https_validate_certificates
            boto_cfg = "\n".join(["[Boto]",
                                  "debug = 0",
                                  "is_secure = %s" % is_secure,
                                  "https_validate_certificates = %s" % vcerts,
                                  "cfn_region_name = chef_validator",
                                  "cfn_region_endpoint = %s" %
                                  cfn_url.hostname,
                                  "cloudwatch_region_name = chef_validator",
                                  "cloudwatch_region_endpoint = %s" %
                                  cw_url.hostname])
            attachments.append((boto_cfg,
                                'cfn-boto-cfg', 'x-cfninitdata'))

        subparts = [make_subpart(*args) for args in attachments]
        mime_blob = multipart.MIMEMultipart(_subparts=subparts)

        return mime_blob.as_string()

    def check_delete_server_complete(self, server_id):
        """Wait for server to disappear from Nova."""
        try:
            server = self.fetch_server(server_id)
        except Exception as exc:
            self.ignore_not_found(exc)
            return True
        if not server:
            return False
        task_state_in_nova = getattr(server, 'OS-EXT-STS:task_state', None)
        # the status of server won't change until the delete task has done
        if task_state_in_nova == 'deleting':
            return False

        status = self.get_status(server)
        if status in ("DELETED", "SOFT_DELETED"):
            return True
        if status == 'ERROR':
            fault = getattr(server, 'fault', {})
            message = fault.get('message', 'Unknown')
            code = fault.get('code')
            errmsg = _(
                "Server %(name)s delete failed: (%(code)s) %(message)s") \
                % dict(name=server.name, code=code, message=message)
            raise resource.ResourceInError(resource_status=status,
                                           status_reason=errmsg)
        return False

    def meta_serialize(self, metadata):
        """Serialize non-string metadata values before sending them to
        Nova.
        """
        if not isinstance(metadata, collections.Mapping):
            raise exception.StackValidationFailed(message=_(
                "nova server metadata needs to be a Map."))

        return dict((key, (value if isinstance(value,
                                               six.string_types)
                           else jsonutils.dumps(value))
                     ) for (key, value) in metadata.items())

    def meta_update(self, server, metadata):
        """Delete/Add the metadata in nova as needed."""
        metadata = self.meta_serialize(metadata)
        current_md = server.metadata
        to_del = [key for key in six.iterkeys(current_md)
                  if key not in metadata]
        client = self.client()
        if len(to_del) > 0:
            client.servers.delete_meta(server, to_del)

        client.servers.set_meta(server, metadata)

    def server_to_ipaddress(self, server):
        """Return the server's IP address, fetching it from Nova."""
        try:
            server = self.client().servers.get(server)
        except exceptions.NotFound as ex:
            LOG.warn(_LW('Instance (%(server)s) not found: %(ex)s'),
                     {'server': server, 'ex': ex})
        else:
            for n in server.networks:
                if len(server.networks[n]) > 0:
                    return server.networks[n][0]

    def absolute_limits(self):
        """Return the absolute limits as a dictionary."""
        limits = self.client().limits.get()
        return dict([(limit.name, limit.value)
                     for limit in list(limits.absolute)])

    def get_console_urls(self, server):
        """Return dict-like structure of server's console urls.

        The actual console url is lazily resolved on access.

        """

        class ConsoleUrls(collections.Mapping):
            def __init__(self, server):
                self.console_methods = {
                    'novnc': server.get_vnc_console,
                    'xvpvnc': server.get_vnc_console,
                    'spice-html5': server.get_spice_console,
                    'rdp-html5': server.get_rdp_console,
                    'serial': server.get_serial_console
                }

            def __getitem__(self, key):
                try:
                    url = self.console_methods[key](key)['console']['url']
                except exceptions.BadRequest as e:
                    unavailable = 'Unavailable console type'
                    if unavailable in e.message:
                        url = e.message
                    else:
                        raise
                return url

            def __len__(self):
                return len(self.console_methods)

            def __iter__(self):
                return (key for key in self.console_methods)

        return ConsoleUrls(server)

    def get_net_id_by_label(self, label):
        try:
            net_id = self.client().networks.find(label=label).id
        except exceptions.NotFound as ex:
            LOG.debug('Nova network (%(net)s) not found: %(ex)s',
                      {'net': label, 'ex': ex})
            raise exception.NovaNetworkNotFound(network=label)
        except exceptions.NoUniqueMatch as exc:
            LOG.debug('Nova network (%(net)s) is not unique matched: %(exc)s',
                      {'net': label, 'exc': exc})
            raise exception.PhysicalResourceNameAmbiguity(name=label)
        return net_id

    def get_nova_network_id(self, net_identifier):
        if uuidutils.is_uuid_like(net_identifier):
            try:
                net_id = self.client().networks.get(net_identifier).id
            except exceptions.NotFound:
                net_id = self.get_net_id_by_label(net_identifier)
        else:
            net_id = self.get_net_id_by_label(net_identifier)

        return net_id
