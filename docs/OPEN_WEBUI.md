# Getting Started

## First Run

- during the first run, Open WebUI will download an embedding model.  This may take a while depending on your internet connection.

## Users

- hit https://yourdomain.ai in your browser
- register the first admin user.  Note that the email address is not verified.
- click on your user icon at the bottom left and select "Admin Panel"
- you can create additional users under the Users tab

## Models

- if you don't yet have inference nodes:
  - add a cloud connection *Settings -> Connections* tabs
  - change some models to public visibility in the *Settings -> Models* tab

## Storage

Open WebUI uses a database for user data.  It also uses a cache in `/rbd/misc/app/open-webui-data`

TODO - use qdrant as a vector database intead of chroma.
