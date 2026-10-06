


# ---------- Rasa server (NLU + dialogue) ----------
# Tag must match the Rasa version you trained with locally: run `rasa --version`
FROM rasa/rasa:3.6.20

WORKDIR /app
USER root

# Only the files Rasa needs (eco_hotels.json belongs to the action server, not here)
COPY config.yml domain.yml credentials.yml /app/
COPY data/nlu.yml data/stories.yml data/rules.yml /app/data/
# Inside Docker the action server is reached by its service name, not localhost
COPY endpoints.docker.yml /app/endpoints.yml

# Train during the build so the container starts instantly with a ready model
RUN rasa train

RUN chown -R 1001:1001 /app
USER 1001

EXPOSE 5005
CMD ["run", "--enable-api", "--cors", "*", "--port", "5005", "--endpoints", "endpoints.yml"]