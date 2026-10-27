FROM python:3.12-slim
RUN pip install --no-cache-dir numpy matplotlib Pillow 'pygame==2.6.1'
RUN useradd --uid 10004 --create-home runner
USER runner
WORKDIR /output
ENV MPLBACKEND=Agg PYTHONDONTWRITEBYTECODE=1 SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy PYGAME_HIDE_SUPPORT_PROMPT=1
