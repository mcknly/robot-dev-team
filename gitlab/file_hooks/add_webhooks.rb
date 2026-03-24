#!/opt/gitlab/embedded/bin/ruby
#
# Robot Dev Team Project
# File: gitlab/file_hooks/add_webhooks.rb
# Description: GitLab File Hook - auto-configures project webhooks on creation.
# License: MIT
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 MCKNLY LLC
#
# GitLab File Hook that automatically adds a webhook to newly created projects.
# Place this script (or mount it) at:
#   /opt/gitlab/embedded/service/gitlab-rails/file_hooks/add_webhooks.rb
#
# Required environment variables (set via /etc/gitlab/gitlab.rb):
#   GITLAB_ADMIN_TOKEN   - Admin or Maintainer PAT with api scope
#   ROBOT_WEBHOOK_URL    - Webhook listener URL (e.g. https://host/webhooks/gitlab)
#
# Optional environment variables:
#   GITLAB_API_URL       - GitLab API base (default: http://localhost:80/api/v4)
#   GITLAB_WEBHOOK_SECRET - Shared secret token for webhook authentication
#   GITLAB_WEBHOOK_SSL_VERIFY - Enable SSL verification on the created webhook
#                               (default: true; set to "false" for self-signed certs)
#
# Validate with: gitlab-rake file_hooks:validate

require 'json'
require 'net/http'
require 'uri'
require 'fileutils'
require 'logger'

# --- CONFIGURATION (from environment) ---
GITLAB_API_URL    = ENV.fetch('GITLAB_API_URL', 'http://localhost:80/api/v4')

# Resolve the admin token: prefer the env var (set via gitlab_rails['env'] in
# gitlab.rb / GITLAB_OMNIBUS_CONFIG), then fall back to /etc/gitlab/rdt.env
# (written by docker-entrypoint-rdt.sh on first boot when the token is
# auto-created and no GITLAB_ADMIN_TOKEN was present in gitlab/.env).
PRIVATE_TOKEN = begin
  token = ENV['GITLAB_ADMIN_TOKEN']
  # Treat absent, empty, or placeholder values as unset.
  if token.nil? || token.empty? || token.start_with?('glpat-xxx')
    rdt_env = '/etc/gitlab/rdt.env'
    if File.exist?(rdt_env)
      line = File.readlines(rdt_env).find { |l| l.start_with?('GITLAB_ADMIN_TOKEN=') }
      token = line&.split('=', 2)&.last&.strip
    end
  end
  token || abort('GITLAB_ADMIN_TOKEN not set and not found in /etc/gitlab/rdt.env')
end

TARGET_WEBHOOK_URL = ENV.fetch('ROBOT_WEBHOOK_URL') { abort 'ROBOT_WEBHOOK_URL not set' }
WEBHOOK_SECRET    = ENV.fetch('GITLAB_WEBHOOK_SECRET', nil)
SSL_VERIFY        = ENV.fetch('GITLAB_WEBHOOK_SSL_VERIFY', 'true').downcase != 'false'
LOG_FILE          = '/var/log/gitlab/file_hooks/add_webhooks.log'

# --- LOGGING ---
logger = begin
  FileUtils.mkdir_p(File.dirname(LOG_FILE))
  l = Logger.new(LOG_FILE, 'weekly')
  l.level = Logger::INFO
  l
rescue StandardError
  l = Logger.new($stderr)
  l.level = Logger::INFO
  l
end

begin
  input = STDIN.read
  exit 0 if input.nil? || input.empty?

  event = JSON.parse(input)

  # Only act on project creation events
  unless event['event_name'] == 'project_create'
    exit 0
  end

  project_id   = event['project_id']
  project_path = event['path_with_namespace']

  logger.info("project_create received: #{project_path} (ID: #{project_id})")

  # --- IDEMPOTENCY CHECK ---
  # Fetch existing hooks and skip if the target URL is already registered
  list_uri = URI("#{GITLAB_API_URL}/projects/#{project_id}/hooks?per_page=100")
  list_http = Net::HTTP.new(list_uri.host, list_uri.port)
  list_http.use_ssl = (list_uri.scheme == 'https')
  list_http.open_timeout = 10
  list_http.read_timeout = 10

  list_req = Net::HTTP::Get.new(list_uri.request_uri)
  list_req['PRIVATE-TOKEN'] = PRIVATE_TOKEN

  list_resp = list_http.request(list_req)
  if list_resp.code.to_i == 200
    existing_hooks = JSON.parse(list_resp.body)
    if existing_hooks.any? { |h| h['url'] == TARGET_WEBHOOK_URL }
      logger.info("SKIP: webhook already exists for #{project_path}")
      exit 0
    end
  else
    logger.warn("Could not list hooks for #{project_path} (HTTP #{list_resp.code}), proceeding with creation")
  end

  # --- CREATE WEBHOOK ---
  webhook_data = {
    url: TARGET_WEBHOOK_URL,
    push_events: false,
    issues_events: true,
    merge_requests_events: true,
    note_events: true,
    confidential_issues_events: true,
    confidential_note_events: true,
    enable_ssl_verification: SSL_VERIFY
  }
  webhook_data[:token] = WEBHOOK_SECRET if WEBHOOK_SECRET && !WEBHOOK_SECRET.empty?

  uri  = URI("#{GITLAB_API_URL}/projects/#{project_id}/hooks")
  http = Net::HTTP.new(uri.host, uri.port)
  http.use_ssl = (uri.scheme == 'https')
  http.open_timeout = 10
  http.read_timeout = 10

  request = Net::HTTP::Post.new(uri.request_uri)
  request['PRIVATE-TOKEN'] = PRIVATE_TOKEN
  request['Content-Type']  = 'application/json'
  request.body = webhook_data.to_json

  response = http.request(request)

  if response.code.to_i >= 200 && response.code.to_i < 300
    logger.info("SUCCESS: webhook added to #{project_path} (HTTP #{response.code})")
  else
    logger.error("FAILED: webhook for #{project_path} (HTTP #{response.code}): #{response.body}")
  end

rescue JSON::ParserError => e
  logger.error("JSON parse error: #{e.message}")
  exit 1
rescue StandardError => e
  logger.error("#{e.class}: #{e.message}")
  exit 1
end
