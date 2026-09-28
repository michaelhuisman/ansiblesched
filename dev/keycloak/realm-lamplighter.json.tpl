{
  "realm": "lamplighter",
  "enabled": true,
  "sslRequired": "none",
  "accessTokenLifespan": 300,
  "roles": {
    "client": {
      "lamplighter": [
        {"name": "viewer", "description": "Read only"},
        {"name": "operator", "description": "Read, launch and cancel"},
        {"name": "admin", "description": "Everything, including configuration and users"}
      ]
    }
  },
  "clients": [
    {
      "clientId": "lamplighter",
      "name": "lamplighter",
      "protocol": "openid-connect",
      "publicClient": false,
      "secret": "__CLIENT_SECRET__",
      "standardFlowEnabled": true,
      "directAccessGrantsEnabled": false,
      "redirectUris": ["http://localhost:8000/ui/auth/callback"],
      "webOrigins": ["http://localhost:8000"],
      "attributes": {
        "pkce.code.challenge.method": "S256",
        "post.logout.redirect.uris": "http://localhost:8000/ui/login"
      },
      "protocolMappers": [
        {
          "name": "audience lamplighter",
          "protocol": "openid-connect",
          "protocolMapper": "oidc-audience-mapper",
          "config": {
            "included.client.audience": "lamplighter",
            "access.token.claim": "true",
            "id.token.claim": "false"
          }
        }
      ]
    },
    {
      "clientId": "lamplighter-tests",
      "name": "Integration tests only (password grant)",
      "protocol": "openid-connect",
      "publicClient": true,
      "standardFlowEnabled": false,
      "directAccessGrantsEnabled": true,
      "fullScopeAllowed": true,
      "protocolMappers": [
        {
          "name": "audience lamplighter",
          "protocol": "openid-connect",
          "protocolMapper": "oidc-audience-mapper",
          "config": {
            "included.client.audience": "lamplighter",
            "access.token.claim": "true",
            "id.token.claim": "false"
          }
        }
      ]
    }
  ],
  "users": [
    {
      "username": "kc-viewer", "enabled": true, "emailVerified": true,
      "email": "kc-viewer@example.invalid", "firstName": "Vera", "lastName": "Viewer",
      "credentials": [{"type": "password", "value": "__PW_VIEWER__", "temporary": false}],
      "clientRoles": {"lamplighter": ["viewer"]}
    },
    {
      "username": "kc-operator", "enabled": true, "emailVerified": true,
      "email": "kc-operator@example.invalid", "firstName": "Otto", "lastName": "Operator",
      "credentials": [{"type": "password", "value": "__PW_OPERATOR__", "temporary": false}],
      "clientRoles": {"lamplighter": ["operator"]}
    },
    {
      "username": "kc-admin", "enabled": true, "emailVerified": true,
      "email": "kc-admin@example.invalid", "firstName": "Ada", "lastName": "Admin",
      "credentials": [{"type": "password", "value": "__PW_ADMIN__", "temporary": false}],
      "clientRoles": {"lamplighter": ["admin"]}
    },
    {
      "username": "kc-norole", "enabled": true, "emailVerified": true,
      "email": "kc-norole@example.invalid", "firstName": "Nora", "lastName": "Norole",
      "credentials": [{"type": "password", "value": "__PW_NOROLE__", "temporary": false}]
    }
  ]
}
