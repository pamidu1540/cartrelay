# Security

Do not post credentials, private keys, customer records, or exploitable vulnerability details in public issues. Use GitHub private vulnerability reporting when available; otherwise contact the maintainer privately before sharing details.

Revoke exposed credentials immediately. Removing them from a commit does not revoke them. Keep environment files, keys, database dumps, and logs outside source control and release archives.

CartRelay assumes a trusted host and private Docker network/socket. Docker administrators can inspect credentials. HMAC does not encrypt transport, and machine-ID-derived configuration encryption does not protect against host administrators.

Set your own retention and access policies for profiles, phone numbers, addresses, and orders. The profile-removal command does not erase all order data or establish regulatory compliance.

Only the current source branch is actively maintained. This experimental project requires deployment-specific review before use with real customers.
