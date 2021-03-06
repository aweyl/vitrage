# Copyright 2017 - Nokia
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import jwt
import os
import requests

from oslo_config import cfg
from oslo_log import log as logging
from oslo_middleware import base
from oslo_serialization import jsonutils
from six.moves import urllib
from webob import exc


LOG = logging.getLogger(__name__)

OPENID_CONNECT_USERINFO = '%s/realms/%s/protocol/openid-connect/userinfo'

KEYCLOAK_GROUP = 'keycloak'
KEYCLOAK_OPTS = [
    cfg.StrOpt('auth_url', default='http://127.0.0.1:9080/auth',
               help='Keycloak authentication server ip',),
    cfg.StrOpt('insecure', default=False,
               help='If True, SSL/TLS certificate verification is disabled'),
    cfg.StrOpt('certfile',
               help='Required if identity server requires client certificate'),
    cfg.StrOpt('keyfile',
               help='Required if identity server requires client certificate'),
    cfg.StrOpt('cafile',
               help='A PEM encoded Certificate Authority to use when verifying'
               ' HTTPs connections. Defaults to system CAs.'),
]


class KeycloakAuth(base.ConfigurableMiddleware):

    def __init__(self, application, conf=None):
        super(KeycloakAuth, self).__init__(application, conf)

        self.oslo_conf.register_opts(KEYCLOAK_OPTS, '%s' % KEYCLOAK_GROUP)
        self.auth_url = self._conf_get('auth_url', KEYCLOAK_GROUP)
        self.insecure = self._conf_get('insecure', KEYCLOAK_GROUP)
        self.certfile = self._conf_get('certfile', KEYCLOAK_GROUP)
        self.keyfile = self._conf_get('keyfile', KEYCLOAK_GROUP)
        self.cafile = self._conf_get('cafile', KEYCLOAK_GROUP) or \
            self._get_system_ca_file()
        self.decoded = {}

    @property
    def reject_auth_headers(self):
        header_val = 'Keycloak uri=\'%s\'' % self.auth_url
        return [('WWW-Authenticate', header_val)]

    @property
    def roles(self):
        return ','.join(self.decoded['realm_access']['roles']) \
            if 'realm_access' in self.decoded else ''

    @property
    def realm_name(self):
        # Get user realm from parsed token
        # Format is "iss": "http://<host>:<port>/auth/realms/<realm_name>",
        __, __, realm_name = self.decoded['iss'].strip().rpartition('/realms/')
        return realm_name

    def process_request(self, req):
            self._authenticate(req)

    def _authenticate(self, req):
        self.token = req.headers.get('X-Auth-Token')
        if self.token:
            self._decode()
        else:
            message = 'Auth token must be provided in "X-Auth-Token" header.'
            self._unauthorized(message)

        self.call_keycloak()

        self._set_req_headers(req)

    def _decode(self):
        try:
            self.decoded = jwt.decode(self.token, algorithms=['RS256'],
                                      verify=False)
        except jwt.DecodeError:
            message = "Token can't be decoded because of wrong format."
            self._unauthorized(message)

    def call_keycloak(self):
        endpoint = OPENID_CONNECT_USERINFO % (self.auth_url, self.realm_name)
        headers = {'Authorization': 'Bearer %s' % self.token}
        verify = None
        if urllib.parse.urlparse(endpoint).scheme == "https":
            verify = False if self.insecure else self.cafile
        cert = (self.certfile, self.keyfile) if self.certfile and self.keyfile \
            else None
        resp = requests.get(endpoint, headers=headers,
                            verify=verify, cert=cert)
        resp.raise_for_status()

    def _set_req_headers(self, req):
        req.headers['X-Identity-Status'] = 'Confirmed'
        req.headers['X-Roles'] = self.roles

    def _unauthorized(self, message):
        body = {'error': {
            'code': 401,
            'title': 'Unauthorized',
            'message': message,
        }}

        raise exc.HTTPUnauthorized(body=jsonutils.dumps(body),
                                   headers=self.reject_auth_headers,
                                   charset='UTF-8',
                                   content_type='application/json')

    @staticmethod
    def _get_system_ca_file():
        """Return path to system default CA file."""
        # Standard CA file locations for Debian/Ubuntu, RedHat/Fedora,
        # Suse, FreeBSD/OpenBSD, MacOSX, and the bundled ca
        ca_path = ['/etc/ssl/certs/ca-certificates.crt',
                   '/etc/pki/tls/certs/ca-bundle.crt',
                   '/etc/ssl/ca-bundle.pem',
                   '/etc/ssl/cert.pem',
                   '/System/Library/OpenSSL/certs/cacert.pem',
                   requests.certs.where()]
        for ca in ca_path:
            LOG.debug("Looking for ca file %s", ca)
            if os.path.exists(ca):
                LOG.debug("Using ca file %s", ca)
                return ca
        LOG.warning("System ca file could not be found.")


filter_factory = KeycloakAuth.factory
