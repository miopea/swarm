# Local secrets

Secret values are stored in the `miopea-secrets` Azure Key Vault. Git contains
only the mapping in `config/secrets.manifest.json`; populated environment files
remain ignored.

## Set up a fresh checkout

1. Install the Azure CLI and Node.js 20 or newer.
2. Sign in with an identity that can read the vault:

   ```bash
   az login
   ```

3. Pull and verify the secrets declared by this repository:

   ```bash
   node scripts/secrets.mjs pull
   node scripts/secrets.mjs check
   ```

The pull command preserves unrelated variables, validates structured values,
writes atomically, and refuses to write to a tracked or non-ignored file.

## Add or rotate a secret

Create or update the value in Azure Key Vault, then add only its vault-to-env
mapping to `config/secrets.manifest.json`. Prefer application-specific,
least-privilege credentials.

Never commit a populated `.env`, private key, service-account JSON, access token,
or decrypted export. Production configuration remains independent until changed
through that service's deployment process.
