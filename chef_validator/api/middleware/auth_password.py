#
# Copyright 2013 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from keystoneclient import exceptions as keystone_exceptions
from keystoneclient import session
from webob import exc
from oslo_log import log as logging

from chef_validator.common.i18n import _LE
from chef_validator.api.middleware import context

LOG = logging.getLogger(__name__)


class KeystonePasswordAuthProtocol(object):
    """
    Alternative authentication middleware that uses username and password
    to authenticate against Keystone instead of validating existing auth token.
    The benefit being that you no longer require admin/service token to
    authenticate users.
    """

    def __init__(self, app, conf):
        self.app = app
        self.conf = conf
        self.session = session.Session()

    def __call__(self, env, start_response):
        """Authenticate incoming request."""
        username = env.get('HTTP_X_AUTH_USER')
        password = env.get('HTTP_X_AUTH_KEY')
        # Determine tenant id from path.
        # tenant = env.get('PATH_INFO').split('/')[1]
        # FIXME tenant is user
        tenant = username
        auth_url = env.get('HTTP_X_AUTH_URL')
        if not tenant:
            return self._reject_request(env, start_response, auth_url)
        try:
            ctx = context.RequestContext(
                username=username,
                password=password,
                tenant=tenant,
                auth_url=auth_url,
                is_admin=False
            )
            auth_ref = ctx.auth_plugin.get_access(self.session)
        except (keystone_exceptions.Unauthorized,
                keystone_exceptions.Forbidden,
                keystone_exceptions.NotFound,
                keystone_exceptions.AuthorizationFailure):
            LOG.error(_LE("Context build failed"))
            return self._reject_request(env, start_response, auth_url)
        env.update(self._build_user_headers(auth_ref))
        return self.app(env, start_response)

    @staticmethod
    def _reject_request(env, start_response, auth_url):
        """Redirect client to auth server."""
        headers = [('WWW-Authenticate', "Keystone uri='%s'" % auth_url)]
        resp = exc.HTTPUnauthorized('Authentication required', headers)
        return resp(env, start_response)

    @staticmethod
    def _build_user_headers(token_info):
        """Build headers that represent authenticated user from auth token."""
        if token_info.get('version') == 'v3':
            keystone_token_info = {'token': token_info}
            tenant_id = token_info['project']['id']
            tenant_name = token_info['project']['name']
            user_id = token_info['user']['id']
            user_name = token_info['user']['name']
            roles = ','.join(
                [role['name'] for role in token_info['roles']])
            service_catalog = None
            auth_token = token_info['auth_token']
        else:
            keystone_token_info = token_info
            tenant_id = token_info['token']['tenant']['id']
            tenant_name = token_info['token']['tenant']['name']
            user_id = token_info['user']['id']
            user_name = token_info['user']['name']
            roles = ','.join(
                [role['name'] for role in token_info['user']['roles']])
            service_catalog = token_info['serviceCatalog']
            auth_token = token_info['token']['id']

        headers = {
            'keystone.token_info': keystone_token_info,
            'HTTP_X_IDENTITY_STATUS': 'Confirmed',
            'HTTP_X_PROJECT_ID': tenant_id,
            'HTTP_X_PROJECT_NAME': tenant_name,
            'HTTP_X_USER_ID': user_id,
            'HTTP_X_USER_NAME': user_name,
            'HTTP_X_ROLES': roles,
            'HTTP_X_SERVICE_CATALOG': service_catalog,
            'HTTP_X_AUTH_TOKEN': auth_token,
        }
        return headers


def filter_factory(global_conf, **local_conf):
    """Returns a WSGI filter app for use with paste.deploy."""
    conf = global_conf.copy()
    conf.update(local_conf)

    def auth_filter(app):
        return KeystonePasswordAuthProtocol(app, conf)

    return auth_filter
