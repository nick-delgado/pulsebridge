<?php
// PulseBridge OpenEMR client tools (Terraform module openemr, task T23).
//
// Runs inside a throwaway container of the OpenEMR image, started by the boot hooks with
// `docker run --rm --network none --user 0:0 --entrypoint php`, with one host directory mounted at
// /work (and, for `bundle`, the TLS certificate read-only). It never prints a private key: keys go
// to mode-600 files in /work, which is on a memory-backed filesystem on the VM, and the hook removes
// them. Only public material (the JWKS, the key ID) is ever written where the hook logs.
//
// Commands:
//   tls-cert <dir> <ipv4> <days>
//       Self-signed certificate for OpenEMR's HTTPS with the VM's address as its subject
//       alternative name (RFC 5280 section 4.2.1.6, iPAddress), so a client that verifies the
//       hostname, as fhir-proxy does, accepts it when it pins this certificate. Writes
//       <dir>/server.key (mode 600) and <dir>/server.crt.
//   keygen <dir>
//       RSA 2048 key pair for the OAuth2 client (spike S1). Writes <dir>/private.pem (PKCS#8,
//       mode 600), <dir>/jwks.json (public key only, RFC 7517) and <dir>/kid. The key ID is the
//       key's RFC 7638 JWK thumbprint, so anyone holding the key can recompute it.
//   register-body <dir> <client_name> <redirect_uri> <scope>
//       Body of the dynamic client registration request (RFC 7591) as S1's recipe: a private
//       client using private_key_jwt and the client_credentials grant only, with the JWKS inline.
//   bundle <dir> <client_id> <site_address> <scope> <certificate>
//       The Secret Manager request that stores what fhir-proxy needs to call OpenEMR:
//       <dir>/add-version.json = {"payload": {"data": base64(<bundle JSON>)}} (mode 600).

declare(strict_types=1);
umask(0077);

const ASSERTION_ALG = 'RS384'; // spike S1: OpenEMR accepted RS384 client assertions

function fail(string $message): void
{
    fwrite(STDERR, "client-tools: $message\n");
    exit(1);
}

function b64url(string $bytes): string
{
    return rtrim(strtr(base64_encode($bytes), '+/', '-_'), '=');
}

function read_file(string $path): string
{
    $data = @file_get_contents($path);
    if ($data === false || $data === '') {
        fail("cannot read $path");
    }
    return $data;
}

// Write through a temporary file and rename, so a half-written file never has the final name.
function write_file(string $path, string $data): void
{
    $tmp = $path . '.tmp';
    if (@file_put_contents($tmp, $data) !== strlen($data) || !@chmod($tmp, 0600) || !@rename($tmp, $path)) {
        @unlink($tmp);
        fail("cannot write $path");
    }
}

function to_json($value): string
{
    return json_encode($value, JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR);
}

function require_dir(string $dir): string
{
    if (!is_dir($dir) || !is_writable($dir)) {
        fail("$dir is not a writable directory");
    }
    return rtrim($dir, '/');
}

function tls_cert(string $dir, string $ip, int $days): void
{
    if (filter_var($ip, FILTER_VALIDATE_IP, FILTER_FLAG_IPV4) === false) {
        fail('tls-cert: not an IPv4 address');
    }
    if ($days < 1 || $days > 3650) {
        fail('tls-cert: days out of range');
    }
    // A self-signed certificate that is its own trust anchor. basicConstraints CA:TRUE with
    // keyCertSign and a critical flag is the shape OpenSSL's strict verification (Python 3.13's
    // default) accepts for a self-signed anchor; extendedKeyUsage limits it to TLS servers and
    // pathlen:0 stops it signing other CAs.
    $config = "$dir/openssl.cnf";
    write_file($config, implode("\n", [
        '[ req ]',
        'distinguished_name = dn',
        'prompt = no',
        '[ dn ]',
        "CN = $ip",
        'O = PulseBridge OpenEMR',
        '[ v3 ]',
        "subjectAltName = IP:$ip",
        'basicConstraints = critical, CA:TRUE, pathlen:0',
        'keyUsage = critical, digitalSignature, keyEncipherment, keyCertSign',
        'extendedKeyUsage = serverAuth',
        'subjectKeyIdentifier = hash',
        'authorityKeyIdentifier = keyid:always',
        '',
    ]));
    $options = ['config' => $config, 'x509_extensions' => 'v3', 'digest_alg' => 'sha256'];
    $key = openssl_pkey_new(['private_key_type' => OPENSSL_KEYTYPE_RSA, 'private_key_bits' => 2048, 'config' => $config]);
    if ($key === false) {
        fail('tls-cert: key generation failed');
    }
    $csr = openssl_csr_new(['commonName' => $ip, 'organizationName' => 'PulseBridge OpenEMR'], $key, $options);
    if ($csr === false) {
        fail('tls-cert: CSR failed');
    }
    $cert = openssl_csr_sign($csr, null, $key, $days, $options, random_int(1, PHP_INT_MAX));
    if ($cert === false || !openssl_x509_export($cert, $certPem) || !openssl_pkey_export($key, $keyPem, null, ['config' => $config])) {
        fail('tls-cert: signing or export failed');
    }
    write_file("$dir/server.key", $keyPem);
    write_file("$dir/server.crt", $certPem);
    @unlink($config);
}

function keygen(string $dir): void
{
    $key = openssl_pkey_new(['private_key_type' => OPENSSL_KEYTYPE_RSA, 'private_key_bits' => 2048]);
    if ($key === false || !openssl_pkey_export($key, $pem)) {
        fail('keygen: key generation failed');
    }
    $details = openssl_pkey_get_details($key);
    if ($details === false || !isset($details['rsa']['n'], $details['rsa']['e'])) {
        fail('keygen: cannot read the public key');
    }
    $n = b64url($details['rsa']['n']);
    $e = b64url($details['rsa']['e']);
    // RFC 7638: the thumbprint is the SHA-256 of the required members in lexicographic order,
    // without whitespace.
    $kid = b64url(hash('sha256', '{"e":"' . $e . '","kty":"RSA","n":"' . $n . '"}', true));
    write_file("$dir/private.pem", $pem);
    write_file("$dir/jwks.json", to_json(['keys' => [[
        'kty' => 'RSA',
        'kid' => $kid,
        'use' => 'sig',
        'alg' => ASSERTION_ALG,
        'n' => $n,
        'e' => $e,
    ]]]));
    write_file("$dir/kid", $kid);
}

function register_body(string $dir, string $name, string $redirect, string $scope): void
{
    $jwks = json_decode(read_file("$dir/jwks.json"), true, 16, JSON_THROW_ON_ERROR);
    // Spike S1, recipe step 4. redirect_uris is required by OpenEMR's endpoint even though the
    // client_credentials grant never redirects.
    write_file("$dir/registration.json", to_json([
        'application_type' => 'private',
        'client_name' => $name,
        'redirect_uris' => [$redirect],
        'token_endpoint_auth_method' => 'private_key_jwt',
        'grant_types' => ['client_credentials'],
        'scope' => $scope,
        'jwks' => $jwks,
    ]));
}

function bundle(string $dir, string $clientId, string $site, string $scope, string $certPath): void
{
    if (preg_match('/^[A-Za-z0-9_-]{16,128}$/', $clientId) !== 1) {
        fail('bundle: unexpected client_id format');
    }
    // The one document fhir-proxy reads (README, "What fhir-proxy gets"). The client ID, key ID,
    // certificate and addresses are not secret; they travel with the key so the proxy always gets
    // a matching set: a new registration replaces all of them in one secret version.
    $document = to_json([
        'format' => 'pulsebridge.openemr-client.v1',
        'client_id' => $clientId,
        'token_endpoint_auth_method' => 'private_key_jwt',
        'kid' => trim(read_file("$dir/kid")),
        'alg' => ASSERTION_ALG,
        'private_key_pem' => read_file("$dir/private.pem"),
        'site_addr_oath' => $site,
        'token_url' => "$site/oauth2/default/token",
        'fhir_base_url' => "$site/apis/default/fhir",
        'scope' => $scope,
        'server_certificate_pem' => read_file($certPath),
        'created' => gmdate('Y-m-d\TH:i:s\Z'),
    ]);
    write_file("$dir/add-version.json", to_json(['payload' => ['data' => base64_encode($document)]]));
}

$command = $argv[1] ?? '';
switch ($command) {
    case 'tls-cert':
        if ($argc !== 5) {
            fail('usage: tls-cert <dir> <ipv4> <days>');
        }
        tls_cert(require_dir($argv[2]), $argv[3], (int) $argv[4]);
        break;
    case 'keygen':
        if ($argc !== 3) {
            fail('usage: keygen <dir>');
        }
        keygen(require_dir($argv[2]));
        break;
    case 'register-body':
        if ($argc !== 6) {
            fail('usage: register-body <dir> <client_name> <redirect_uri> <scope>');
        }
        register_body(require_dir($argv[2]), $argv[3], $argv[4], $argv[5]);
        break;
    case 'bundle':
        if ($argc !== 7) {
            fail('usage: bundle <dir> <client_id> <site_address> <scope> <certificate>');
        }
        bundle(require_dir($argv[2]), $argv[3], $argv[4], $argv[5], $argv[6]);
        break;
    default:
        fail('unknown command');
}
