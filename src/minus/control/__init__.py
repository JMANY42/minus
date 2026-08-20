"""The control channel: how a running MINUS is inspected and driven.

Deliberately empty. Importing `minus.control.protocol` should not drag in the
socket server, and importing the server should not be the price of parsing one
frame -- the dashboard needs the client, the assistant needs the server, and
tests want the codec on its own.
"""
