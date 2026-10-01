"""Version-control backends.

One run talks to the repository's forge through a set of role protocols
(:mod:`lantern.vcs.protocol`) in shared, forge-neutral types
(:mod:`lantern.vcs.model`); a backend package — ``github/`` today — implements
the roles against its own API and keeps its generic transport to itself.
"""
