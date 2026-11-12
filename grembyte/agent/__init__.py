"""The base every Frida agent of grembyte builds on, running inside the game.

``source``
    Loads an agent's JavaScript from the .js file beside its module, and
    ``MESSAGES_SOURCE`` (messages.js): errors grembyte renders in the user's
    language.
``platform``
    What differs between Linux and Windows (Wine/Proton) inside the game.

Not to be confused with :mod:`grembyte.platform`, which is grembyte's own side
of a process: reading its memory and mappings from outside.
"""
