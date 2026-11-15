"""Meadow: browse the Blossom media of a Nostr social graph.

Enter an npub, Meadow walks the graph (kind 3 follows via kind 10002 relay
lists), collects each author's kind 10063 media-server list, queries every
Blossom server (BUD-02 /list) for blobs authored by the people who listed
that server, and displays the result as a paginated media feed.
"""
__version__ = '0.1.1'
