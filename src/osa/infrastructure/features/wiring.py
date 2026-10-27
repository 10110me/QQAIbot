from pathlib import Path

from osa.application.features.consolidation import ConsolidationService
from osa.application.features.hub import FeatureHub
from osa.application.features.knowledge import KnowledgeService
from osa.application.features.media import MediaService
from osa.application.features.research import ResearchService
from osa.application.features.tasks import TaskService
from osa.application.features.plugins import PluginService
from osa.infrastructure.features.clients import BrowserWorkerClient, SearchClient
from osa.infrastructure.features.downloader import PublicDownloader
from osa.infrastructure.features.embedding import APIEmbedder, FakeEmbedder
from osa.infrastructure.features.executor import SandboxClient, ScreenshotClient
from osa.infrastructure.features.media_provider import OpenAIMediaProvider, RoutedMediaProvider
from osa.infrastructure.features.objects import LocalBlobStore, S3BlobStore
from osa.infrastructure.features.parser_client import ParserWorkerClient
from osa.infrastructure.features.repository import MemoryFeatureRepository
from osa.infrastructure.features.social import QZoneAdapter
from osa.infrastructure.features.video_client import VideoWorkerClient
from osa.infrastructure.features.qqbot import QqbotToolClient


def wire_features(settings, store, model, resources, platform):
    if settings.database_url == "memory://":
        repo = MemoryFeatureRepository()
    else:
        from osa.infrastructure.features.sql_repository import SQLFeatureRepository
        repo = SQLFeatureRepository(store)
    objects = S3BlobStore(settings.s3_endpoint, settings.s3_bucket) if settings.object_store == "s3" else LocalBlobStore(str(Path(settings.data_dir)/"objects"))
    downloader = PublicDownloader(settings.max_download_bytes)
    embedding = FakeEmbedder() if settings.demo_mode else (APIEmbedder(settings.embedding_base_url,
        settings.embedding_api_key.get_secret_value() or settings.model_api_key.get_secret_value(),
        settings.embedding_model, settings.embedding_dimension) if settings.embedding_base_url else None)
    knowledge = KnowledgeService(repo, embedding)
    worker_key = settings.worker_token.get_secret_value()
    browser = BrowserWorkerClient(settings.browser_worker_url, worker_key)
    search = (None if settings.search_provider == "none" else
              SearchClient(settings.search_provider, settings.search_base_url, settings.search_api_key.get_secret_value()))
    research = ResearchService(repo, store, browser, search, settings.max_daily_research_calls,
                               defer_human_wait=settings.task_concurrency == 1)
    provider = OpenAIMediaProvider(settings.model_base_url, settings.model_api_key.get_secret_value(),
                                   settings.vision_model, settings.audio_model, settings.tts_model)
    if settings.model_provider != 'fake':provider=RoutedMediaProvider(model,provider)
    video = VideoWorkerClient(settings.indexer_worker_url, worker_key)
    media = MediaService(repo, objects, downloader, provider, video)
    consolidation = ConsolidationService(repo, store, model)
    parser = ParserWorkerClient(settings.indexer_worker_url, worker_key)
    sandbox = SandboxClient(settings.sandbox_worker_url, worker_key)
    social = QZoneAdapter(settings.qzone_adapter_url, settings.qzone_adapter_token.get_secret_value()) if settings.qzone_adapter_url else None
    hub = FeatureHub(settings, repo, store, TaskService(repo), knowledge, media, research,
        consolidation, parser, downloader, objects, sandbox, resources, platform, model, social)
    hub.qqbot_tools = QqbotToolClient(settings.qqbot_api_url, timeout=settings.qqbot_timeout,
                                      max_bytes=settings.max_download_bytes,
                                      cache_seconds=settings.qqbot_tools_cache_seconds, repo=repo)
    hub.plugins = PluginService(repo, store, sandbox)
    hub.screenshots = ScreenshotClient(settings.screenshot_worker_url, worker_key)
    hub.tasks.handlers["job.screenshot"] = hub.run_screenshot
    model.repo = repo
    return hub
