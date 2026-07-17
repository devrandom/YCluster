# Split passwords frontend from general, rotate general

Separate the `general` password database from the `frontend` (web
services) one so the blast radius of a leak is smaller, and rotate
`general` as part of the split.
