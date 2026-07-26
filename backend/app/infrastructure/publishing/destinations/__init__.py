"""Destination adapters for the Publishing bounded context (α8.6b+).

Each adapter implements :class:`app.application.interfaces.destination_publisher.IDestinationPublisher`
— it uploads a finished artifact to one platform and is **credential-blind** (PUB-5): it
receives a short-lived ``AuthorizedContext`` bearer, never the credential store. α8.6b ships
:class:`MockDestination`; the real YouTube adapter is α8.6c (a new leaf, no port change).
"""
