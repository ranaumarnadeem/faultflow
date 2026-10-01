"""Inserting MBIST into a user's chip or block RTL, driven by an insertion file.

The insertion file (YAML or JSON) names the design's sources and every memory
instance to wrap; FaultFlow never guesses which memories to test. ``ff.py
list-memories`` reads the design and lists the memory instances it finds, with how
each pin is connected, to help write that file.
"""
