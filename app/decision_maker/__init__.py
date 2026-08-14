"""
app/decision_maker/
--------------------
Layer 6 -- pure logic, zero I/O. Every function here answers a question using only the
facts it's already been given; nothing in this package ever calls a database, an HTTP
client, or `await`s anything. If a function needs data from outside, it belongs in
app/messengers/ instead. See docs/architecture-components.md.
"""
