ARG MIAIR_IMAGE=miair-next:local
FROM ${MIAIR_IMAGE} AS base

FROM base AS enabled
RUN printf '\nfrom app.music_application import register as register_music_application\nregister_music_application(protected_router, public_router)\n' >> /app/backend/app/api/v1/__init__.py

FROM enabled AS custom
COPY --from=application /app.py /app/backend/app/music_application.py

FROM enabled AS fnmusic
COPY integrations/music/fnmusic.py /app/backend/app/music_application.py
COPY integrations/music/fnmusic.html /app/backend/app/fnmusic.html

FROM base AS standard
