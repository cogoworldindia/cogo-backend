{{/*
The shared per-deployable templates. Every Deployment in the chart is
  include "cogo.deployment" (dict "root" $ "dp" <one item of cogo.deployables>)
*/}}

{{/* ------------------------------------------------------------------ env */}}

{{- define "cogo.env" -}}
{{- $root := .root -}}
{{- $svc := .dp.svc -}}
{{- $cfg := .dp.cfg -}}
{{- $env := include "cogo.deepMerge" (dict "dst" ($svc.env | default dict) "src" ($cfg.env | default dict)) | fromYaml -}}
{{- /* ADMIN_BOOTSTRAP_* is development only (docker-compose); never render it. */ -}}
{{- $names := keys $env -}}
{{- range $e := ($svc.secretEnv | default list) -}}
{{- if kindIs "string" $e -}}
{{- $names = append $names $e -}}
{{- else -}}
{{- $names = append $names $e.name -}}
{{- end -}}
{{- end -}}
{{- range $e := concat ($svc.extraEnv | default list) ($cfg.extraEnv | default list) -}}
{{- $names = append $names $e.name -}}
{{- end -}}
{{- range $n := $names -}}
{{- if hasPrefix "ADMIN_BOOTSTRAP_" $n -}}
{{- fail (printf "%s: %s is development only; don't set it outside docker-compose" $.dp.name $n) -}}
{{- end -}}
{{- end -}}
{{- if hasKey $env "APP_CHECK_MODE" -}}
{{- $mode := get $env "APP_CHECK_MODE" -}}
{{- if kindIs "bool" $mode -}}
{{- fail "APP_CHECK_MODE: quote the value (\"off\"); unquoted off is a YAML boolean" -}}
{{- end -}}
{{- if and (ne (toString $mode) "off") (not (get $env "APP_CHECK_PROJECT_NUMBER")) -}}
{{- fail (printf "APP_CHECK_PROJECT_NUMBER is required when APP_CHECK_MODE=%s" $mode) -}}
{{- end -}}
{{- end -}}
{{- /* 1. Secrets. The DB password comes first: DATABASE_URL expands $(<passwordKey>). */ -}}
{{- if $svc.database }}
{{ include "cogo.dbPasswordEnv" (dict "svc" $svc) }}
{{- end }}
{{- range $e := ($svc.secretEnv | default list) }}
{{- $item := ternary (dict "name" $e) $e (kindIs "string" $e) }}
- name: {{ $item.name }}
  valueFrom:
    secretKeyRef:
      name: {{ $item.secretName | default $svc.secretName | required (printf "services.%s.secretName is required for %s" $.dp.svcKey $item.name) }}
      key: {{ $item.key | default $item.name }}
      {{- if $item.optional }}
      optional: true
      {{- end }}
{{- end }}
{{- with $root.Values.sentry.secretName }}
- name: SENTRY_DSN
  valueFrom:
    secretKeyRef:
      name: {{ . }}
      key: {{ $root.Values.sentry.key }}
{{- end }}
{{- if $svc.rabbitmq }}
- name: RABBITMQ_URL
  valueFrom:
    secretKeyRef:
      name: {{ required "rabbitmq.existingSecret is required" $root.Values.rabbitmq.existingSecret }}
      key: {{ $root.Values.rabbitmq.urlKey }}
{{- end }}
{{- /* 2. Infrastructure URLs. */ -}}
{{- if $svc.database }}
- name: DATABASE_URL
  value: {{ include "cogo.databaseUrl" (dict "root" $root "db" $svc.database "direct" false) | quote }}
{{- with $root.Values.postgres.poolSize }}
- name: DB_POOL_SIZE
  value: {{ include "cogo.envValue" . | quote }}
{{- end }}
{{- with $root.Values.postgres.maxOverflow }}
- name: DB_MAX_OVERFLOW
  value: {{ include "cogo.envValue" . | quote }}
{{- end }}
{{- if and $root.Values.pgbouncer.enabled (eq (int $root.Values.pgbouncer.maxPreparedStatements) 0) }}
{{- /* PgBouncer can't track prepared statements: services run without them */}}
- name: DB_PREPARED_STATEMENTS
  value: "false"
{{- end }}
{{- include "cogo.envMap" $root.Values.maintenance }}
{{- end }}
{{- with $svc.redis }}
{{ include "cogo.redisEnv" (dict "root" $root "env" "REDIS_URL" "role" .) }}
{{- end }}
{{- if $svc.realtimeRedis }}
{{ include "cogo.redisEnv" (dict "root" $root "env" "REALTIME_REDIS_URL" "role" "realtime") }}
{{- end }}
{{ include "cogo.redisEnv" (dict "root" $root "env" "CONFIG_REDIS_URL" "role" "config") }}
{{- /* 3. Common settings (docker-compose x-common-env). */ -}}
{{- include "cogo.envMap" $root.Values.commonEnv }}
{{- /* 4. Tokens and keys. */ -}}
{{- if or $svc.jwks $svc.jwtPrivateKey }}
- name: JWT_ISSUER
  value: {{ $root.Values.jwt.issuer | quote }}
- name: JWT_AUDIENCE
  value: {{ $root.Values.jwt.audience | quote }}
{{- end }}
{{- if $svc.jwks }}
- name: AUTH_JWKS_URL
  value: {{ printf "%s/.well-known/jwks.json" (include "cogo.internalUrl" (dict "root" $root "target" $root.Values.jwt.jwksFrom)) | quote }}
{{- end }}
{{- if $svc.jwtPrivateKey }}
- name: JWT_PRIVATE_KEY_FILE
  value: /run/secrets/jwt_private_key
{{- end }}
{{- range $f := ($svc.files | default list) }}
{{- with $f.env }}
- name: {{ . }}
  value: {{ printf "/run/secrets/%s" $f.path | quote }}
{{- end }}
{{- end }}
{{- /* 5. Other services (in-cluster Service URLs). */ -}}
{{- range $k, $target := ($svc.internalUrls | default dict) }}
- name: {{ $k }}
  value: {{ include "cogo.internalUrl" (dict "root" $root "target" $target) | quote }}
{{- end }}
{{- /* 6. The service's settings, then this deployable's flags. */ -}}
{{- include "cogo.envMap" $env }}
{{- with $cfg.metricsPort }}
- name: METRICS_PORT
  value: {{ include "cogo.envValue" . | quote }}
{{- end }}
- name: WEB_CONCURRENCY
  value: {{ include "cogo.envValue" ($cfg.webConcurrency | default $root.Values.pod.webConcurrency) | quote }}
{{- with $svc.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- with $cfg.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/* ------------------------------------------------------------------ secret files */}}

{{/* Projected sources for /run/secrets (JWT key, service key, extra files). */}}
{{- define "cogo.secretSources" -}}
{{- $root := .root -}}
{{- $svc := .dp.svc -}}
{{- if $svc.jwtPrivateKey }}
- secret:
    name: {{ $root.Values.jwt.privateKey.secretName }}
    items:
      - key: {{ $root.Values.jwt.privateKey.key }}
        path: jwt_private_key
{{- end }}
{{- if $svc.serviceKey }}
- secret:
    name: {{ printf "%s%s" $root.Values.serviceKeys.privateKeySecretPrefix (required "serviceName is required" $svc.serviceName) }}
    items:
      - key: {{ $root.Values.serviceKeys.privateKeySecretKey }}
        path: service_key
{{- end }}
{{- range $f := ($svc.files | default list) }}
- secret:
    name: {{ $f.secretName }}
    items:
      - key: {{ $f.key }}
        path: {{ $f.path }}
    {{- if $f.optional }}
    optional: true
    {{- end }}
{{- end }}
{{- end -}}

{{/* ------------------------------------------------------------------ Deployment */}}

{{- define "cogo.deployment" -}}
{{- $root := .root -}}
{{- $dp := .dp -}}
{{- $svc := $dp.svc -}}
{{- $cfg := $dp.cfg -}}
{{- $pod := $root.Values.pod -}}
{{- $fullName := include "cogo.componentName" (dict "root" $root "name" $dp.name) -}}
{{- $selector := include "cogo.selectorLabels" (dict "root" $root "component" $dp.name) -}}
{{- $sources := include "cogo.secretSources" . | trim -}}
{{- $probes := $pod.probes -}}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ $fullName }}
  labels:
    {{- include "cogo.componentLabels" (dict "root" $root "component" $dp.name "role" $cfg.role "service" $dp.svcKey) | nindent 4 }}
spec:
  {{- if not (or (include "cogo.usesHpa" (dict "root" $root "cfg" $cfg)) (include "cogo.usesKeda" (dict "root" $root "cfg" $cfg))) }}
  replicas: {{ $cfg.replicas | default 1 | int }}
  {{- end }}
  revisionHistoryLimit: 5
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 25%
      maxUnavailable: 0
  selector:
    matchLabels:
      {{- $selector | nindent 6 }}
  template:
    metadata:
      labels:
        {{- include "cogo.componentLabels" (dict "root" $root "component" $dp.name "role" $cfg.role "service" $dp.svcKey) | nindent 8 }}
        {{- with $pod.labels }}
        {{- toYaml . | nindent 8 }}
        {{- end }}
      {{- $annotations := include "cogo.deepMerge" (dict "dst" ($pod.annotations | default dict) "src" ($cfg.podAnnotations | default dict)) | fromYaml }}
      {{- if $root.Values.metrics.podAnnotations }}
      {{- $_ := set $annotations "prometheus.io/scrape" "true" }}
      {{- $_ := set $annotations "prometheus.io/path" "/metrics" }}
      {{- $_ := set $annotations "prometheus.io/port" (include "cogo.envValue" ($cfg.metricsPort | default (include "cogo.port" $root))) }}
      {{- end }}
      {{- with $annotations }}
      annotations:
        {{- toYaml . | nindent 8 }}
      {{- end }}
    spec:
      serviceAccountName: {{ include "cogo.serviceAccountName" $root }}
      automountServiceAccountToken: false
      {{- with $root.Values.imagePullSecrets }}
      imagePullSecrets:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      securityContext:
        {{- toYaml $pod.securityContext | nindent 8 }}
      terminationGracePeriodSeconds: {{ $cfg.terminationGracePeriodSeconds | default $pod.terminationGracePeriodSeconds | int }}
      {{- if $pod.topologySpread.enabled }}
      topologySpreadConstraints:
        {{- range $key := $pod.topologySpread.topologyKeys }}
        - maxSkew: {{ $pod.topologySpread.maxSkew | int }}
          topologyKey: {{ $key }}
          whenUnsatisfiable: {{ $pod.topologySpread.whenUnsatisfiable }}
          labelSelector:
            matchLabels:
              {{- $selector | nindent 14 }}
        {{- end }}
      {{- end }}
      {{- with ($cfg.nodeSelector | default $pod.nodeSelector) }}
      nodeSelector:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with ($cfg.tolerations | default $pod.tolerations) }}
      tolerations:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with ($cfg.affinity | default $pod.affinity) }}
      affinity:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      containers:
        - name: {{ $dp.name }}
          image: {{ include "cogo.image" (dict "root" $root "svc" $svc) | quote }}
          imagePullPolicy: {{ $root.Values.image.pullPolicy }}
          {{- with $svc.args }}
          args:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          ports:
            - name: http
              containerPort: {{ include "cogo.port" $root }}
              protocol: TCP
            {{- with $cfg.metricsPort }}
            - name: metrics
              containerPort: {{ . | int }}
              protocol: TCP
            {{- end }}
          env:
            {{- include "cogo.env" . | trim | nindent 12 }}
          startupProbe:
            httpGet: { path: {{ $probes.startup.path }}, port: http }
            periodSeconds: {{ $probes.startup.periodSeconds | int }}
            timeoutSeconds: {{ $probes.startup.timeoutSeconds | int }}
            failureThreshold: {{ $probes.startup.failureThreshold | int }}
          livenessProbe:
            httpGet: { path: {{ $probes.liveness.path }}, port: http }
            periodSeconds: {{ $probes.liveness.periodSeconds | int }}
            timeoutSeconds: {{ $probes.liveness.timeoutSeconds | int }}
            failureThreshold: {{ $probes.liveness.failureThreshold | int }}
          readinessProbe:
            httpGet: { path: {{ $probes.readiness.path }}, port: http }
            periodSeconds: {{ $probes.readiness.periodSeconds | int }}
            timeoutSeconds: {{ $probes.readiness.timeoutSeconds | int }}
            failureThreshold: {{ $probes.readiness.failureThreshold | int }}
          lifecycle:
            preStop:
              exec:
                command: ["sleep", "{{ $pod.preStopSleepSeconds | int }}"]
          resources:
            {{- toYaml ($cfg.resources | default dict) | nindent 12 }}
          securityContext:
            {{- toYaml $pod.containerSecurityContext | nindent 12 }}
          volumeMounts:
            - name: tmp
              mountPath: /tmp
            {{- if $sources }}
            - name: secrets
              mountPath: /run/secrets
              readOnly: true
            {{- end }}
            {{- if $svc.publicKeys }}
            - name: service-keys
              mountPath: /run/service-keys
              readOnly: true
            {{- end }}
      volumes:
        - name: tmp
          emptyDir:
            sizeLimit: {{ $pod.tmpSizeLimit }}
        {{- if $sources }}
        - name: secrets
          projected:
            defaultMode: 288  # 0440: owner and fsGroup (10001) read
            sources:
              {{- $sources | nindent 14 }}
        {{- end }}
        {{- if $svc.publicKeys }}
        - name: service-keys
          secret:
            secretName: {{ $root.Values.serviceKeys.publicKeysSecret }}
            defaultMode: 292  # 0444: public keys
        {{- end }}
{{- end -}}
