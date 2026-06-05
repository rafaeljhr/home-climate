cd gree-test

docker compose up -d --build   # build + start both, detached
docker compose ps              # status
docker compose logs -f         # follow logs (Ctrl-C to detach)

docker compose stop            # stop (keep containers)
docker compose start           # start again
docker compose down            # stop + remove containers (data volume persists)
docker compose restart         # restart both
