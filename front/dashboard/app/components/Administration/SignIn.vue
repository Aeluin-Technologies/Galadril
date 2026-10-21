<script setup lang="ts">
import { toRefs, type UnwrapRef } from "vue";
import type { useAdministration } from "~/composables/useAdministration";

type Controller = Pick<
  UnwrapRef<ReturnType<typeof useAdministration>>,
  | "settings"
  | "busy"
  | "changeMfa"
  | "disable"
  | "providerStep"
  | "saveConnection"
  | "domain"
  | "domainError"
  | "normalizeDomainInput"
  | "challenge"
  | "requestDomain"
  | "detectionAddress"
  | "detectedProvider"
  | "detectProvider"
  | "useDetectedProvider"
  | "prefilled"
  | "protocol"
  | "issuer"
  | "issuerError"
  | "normalizeIssuerInput"
  | "clientId"
  | "secret"
  | "registrationToken"
  | "metadata"
  | "providerAddressValid"
  | "reviewConnection"
  | "jit"
>;
const props = defineProps<{ controller: Controller }>();
const {
  settings,
  busy,
  changeMfa,
  disable,
  providerStep,
  saveConnection,
  domain,
  domainError,
  normalizeDomainInput,
  challenge,
  requestDomain,
  detectionAddress,
  detectedProvider,
  detectProvider,
  useDetectedProvider,
  prefilled,
  protocol,
  issuer,
  issuerError,
  normalizeIssuerInput,
  clientId,
  secret,
  registrationToken,
  metadata,
  providerAddressValid,
  reviewConnection,
  jit,
} = toRefs(props.controller);
</script>
<template>
  <template v-if="settings">
    <UiCard class="">
      <h2>{{ $t("administration.copy.an_extra_check_after_sign_in") }}</h2>
      <p>
        {{
          $t("administration.copy.a_passkey_uses_a_fingerprint_face_device_pin")
        }}
      </p>
      <label class="check mt-5"
        ><UiCheckbox
          type="checkbox"
          :checked="settings.passkey_mfa"
          :disabled="busy"
          @click.prevent="changeMfa(!settings.passkey_mfa)"
        />{{ $t("administration.copy.require_a_passkey_for_everyone") }}</label
      >
      <p class="hint">
        {{
          $t("administration.copy.optional_and_off_by_default_people_without_a")
        }}
      </p> </UiCard
    ><UiCard v-if="settings.connections.length" class="">
      <h2>{{ $t("administration.copy.connected_domains") }}</h2>
      <div
        v-for="connection in settings.connections"
        :key="connection.domain"
        class="flex flex-wrap items-center justify-between gap-3 border-b border-border py-4 last:border-0"
      >
        <div>
          <strong class="text-sm font-medium text-foreground">{{
            connection.domain
          }}</strong>
          <p class="hint">
            {{
              connection.enabled
                ? $t("administration.copy.your_team_can_sign_in")
                : $t("administration.copy.new_sign_ins_are_disabled")
            }}
          </p>
        </div>
        <UiButton
          v-if="connection.enabled"
          type="button"
          variant="secondary"
          @click="disable(connection.domain)"
          :loading="busy"
          >{{ $t("administration.copy.disconnect") }}</UiButton
        >
      </div> </UiCard
    ><UiCard class="">
      <h2>{{ $t("administration.copy.connect_company_sign_in") }}</h2>
      <p>{{ $t("administration.copy.your_it_team_may_need_to_help_with") }}</p>
      <ol
        :aria-label="$t('administration.copy.sign_in_setup_progress')"
        class="my-6 flex gap-2 text-xs"
      >
        <li
          v-for="(step, index) in [
            $t('administration.copy.your_domain'),
            $t('administration.copy.your_provider'),
            $t('administration.copy.review'),
          ]"
          :key="step"
          :aria-current="providerStep === index + 1 ? 'step' : undefined"
          :class="[
            'flex-1 rounded-lg px-3 py-2',
            providerStep === index + 1
              ? 'bg-primary-subtle text-primary-foreground ring-1 ring-focus'
              : 'bg-canvas text-muted',
          ]"
        >
          {{ index + 1 }}. {{ step }}
        </li>
      </ol>
      <form @submit.prevent="saveConnection">
        <fieldset :disabled="busy">
          <div v-if="providerStep === 1">
            <h3>
              {{
                $t("administration.copy.what_email_domain_does_your_team_use")
              }}
            </h3>
            <p>
              {{
                $t(
                  "administration.copy.enter_your_domain_work_email_or_company_website",
                )
              }}
            </p>
            <label for="sso-domain">{{
              $t("administration.copy.company_email_domain")
            }}</label
            ><UiInput
              id="sso-domain"
              v-model="domain"
              required
              maxlength="2048"
              :placeholder="
                $t('administration.copy.company_com_or_alex_company_com')
              "
              autocapitalize="none"
              spellcheck="false"
              :aria-invalid="Boolean(domainError)"
              aria-describedby="sso-domain-hint"
              @blur="domain && normalizeDomainInput()"
              @input="challenge = undefined"
            />
            <p
              id="sso-domain-hint"
              class="hint"
              :role="domainError ? 'alert' : undefined"
            >
              {{
                domainError ||
                $t(
                  "administration.copy.for_alex_company_com_we_use_company_com",
                )
              }}
            </p>
            <UiButton
              type="button"
              variant="secondary"
              :disabled="!domain"
              @click="requestDomain"
              class="mt-4 me-3 mb-1"
              >{{
                $t("administration.copy.get_verification_instructions")
              }}</UiButton
            >
            <div v-if="challenge" class="mt-5 rounded-xl bg-canvas p-4">
              <h3>
                {{
                  $t(
                    "administration.copy.ask_the_person_managing_your_domain_to_add",
                  )
                }}
              </h3>
              <p class="hint">
                {{
                  $t("administration.copy.in_your_domain_s_dns_settings_add_a")
                }}
              </p>
              <dl class="mt-3 space-y-2 text-xs">
                <dt class="text-muted">
                  {{ $t("administration.copy.record_name") }}
                </dt>
                <dd
                  class="break-all rounded border border-border bg-surface p-2 font-mono"
                >
                  {{ challenge.record }}
                </dd>
                <dt class="text-muted">
                  {{ $t("builder.config_modal.card.value") }}
                </dt>
                <dd
                  class="break-all rounded border border-border bg-surface p-2 font-mono"
                >
                  {{ challenge.value }}
                </dd>
              </dl>
            </div>
            <UiButton
              type="button"
              :disabled="!challenge"
              @click="providerStep = 2"
              variant="primary"
              class="mt-5"
              >{{ $t("authentication.continue") }}</UiButton
            >
          </div>
          <div v-else-if="providerStep === 2">
            <h3>
              {{ $t("administration.copy.where_does_your_team_sign_in") }}
            </h3>
            <p>
              {{
                $t(
                  "administration.copy.your_it_team_can_provide_the_address_and",
                )
              }}
            </p>
            <label for="sso-detection">{{
              $t("administration.copy.provider_or_metadata_address")
            }}</label>
            <UiInput
              id="sso-detection"
              v-model.trim="detectionAddress"
              type="text"
              inputmode="url"
              maxlength="2048"
              autocapitalize="none"
              spellcheck="false"
              :placeholder="
                $t('administration.copy.login_company_com_or_a_metadata_url')
              "
              @input="detectedProvider = undefined"
            />
            <p class="hint">
              {{
                $t(
                  "administration.copy.we_check_https_detect_openid_connect_or_saml",
                )
              }}
            </p>
            <UiButton
              type="button"
              variant="secondary"
              @click="detectProvider"
              :loading="busy"
              :disabled="!detectionAddress"
              class="mt-4"
              >{{
                busy
                  ? $t("administration.copy.checking_provider")
                  : $t("administration.copy.detect_configuration")
              }}</UiButton
            >
            <div
              v-if="detectedProvider"
              class="mt-5 rounded-xl border border-border bg-primary-subtle/50 p-5"
              role="region"
              :aria-label="$t('administration.copy.detected_provider')"
            >
              <h3>
                {{
                  detectedProvider.configuration.protocol === "oidc"
                    ? $t("administration.copy.openid_connect_provider_found")
                    : $t("administration.copy.saml_provider_found")
                }}
              </h3>
              <dl class="mt-3 space-y-2 text-sm">
                <dt class="text-muted">
                  {{ $t("administration.copy.provider_identity") }}
                </dt>
                <dd class="break-all">
                  {{
                    detectedProvider.configuration.issuer ||
                    detectedProvider.configuration.entity_id
                  }}
                </dd>
                <dt class="text-muted">
                  {{ $t("administration.copy.company_domain") }}
                </dt>
                <dd>
                  {{ detectedProvider.domain }} ·
                  {{
                    detectedProvider.domain_verified
                      ? $t("administration.copy.ownership_verified")
                      : $t("administration.copy.dns_verification_still_pending")
                  }}
                </dd>
                <template
                  v-if="detectedProvider.configuration.protocol === 'oidc'"
                  ><dt class="text-muted">
                    {{ $t("administration.copy.application_setup") }}
                  </dt>
                  <dd>
                    {{
                      detectedProvider.configuration.automatic_registration
                        ? $t(
                            "administration.copy.automatic_registration_is_available_your_provider_may_require",
                          )
                        : $t(
                            "administration.copy.your_it_team_must_supply_an_application_client",
                          )
                    }}
                  </dd></template
                >
                <template v-else
                  ><dt class="text-muted">
                    {{ $t("administration.copy.signing_certificates") }}
                  </dt>
                  <dd>
                    {{ detectedProvider.configuration.signing_certificates }}
                    {{ $t("administration.copy.current_certificate_s_found") }}
                  </dd></template
                >
              </dl>
              <p class="hint">
                {{
                  $t(
                    "administration.copy.fill_in_the_detected_settings_you_can_review",
                  )
                }}
              </p>
              <div class="mt-4 flex flex-wrap gap-3">
                <UiButton
                  type="button"
                  @click="useDetectedProvider"
                  variant="primary"
                  >{{
                    $t("administration.copy.use_detected_settings")
                  }}</UiButton
                ><UiButton
                  type="button"
                  variant="secondary"
                  @click="detectedProvider = undefined"
                  >{{ $t("administration.copy.keep_manual_setup") }}</UiButton
                >
              </div>
            </div>
            <details
              :open="prefilled"
              class="mt-6 rounded-xl border border-border p-4"
            >
              <summary
                class="cursor-pointer text-sm font-medium text-foreground"
              >
                {{
                  prefilled
                    ? $t("administration.copy.review_detected_settings")
                    : $t("administration.copy.enter_settings_manually")
                }}
              </summary>
              <label for="sso-protocol">{{
                $t("administration.copy.connection_type")
              }}</label
              ><UiSelect
                id="sso-protocol"
                v-model="protocol"
                :options="[
                  {
                    label: $t(
                      'administration.copy.microsoft_google_okta_or_another_openid_provider',
                    ).trim(),
                    value: 'oidc',
                  },
                  {
                    label: $t(
                      'administration.copy.saml_configured_by_your_it_team',
                    ).trim(),
                    value: 'saml',
                  },
                ]"
              /><template v-if="protocol === 'oidc'"
                ><label for="sso-issuer">{{
                  $t("administration.copy.company_sign_in_address")
                }}</label
                ><UiInput
                  id="sso-issuer"
                  v-model="issuer"
                  type="text"
                  inputmode="url"
                  autocapitalize="none"
                  spellcheck="false"
                  required
                  maxlength="2048"
                  :placeholder="$t('administration.copy.login_company_com')"
                  :aria-invalid="Boolean(issuerError)"
                  aria-describedby="sso-issuer-hint"
                  @blur="issuer && normalizeIssuerInput()" />
                <p
                  id="sso-issuer-hint"
                  class="hint"
                  :role="issuerError ? 'alert' : undefined"
                >
                  {{
                    issuerError ||
                    $t(
                      "administration.copy.paste_a_sign_in_or_openid_discovery_address",
                    )
                  }}
                </p>
                <details class="mt-5 rounded-xl border border-border p-4">
                  <summary
                    class="cursor-pointer text-sm font-medium text-foreground"
                  >
                    {{
                      $t(
                        "administration.copy.my_provider_requires_app_credentials",
                      )
                    }}
                  </summary>
                  <p class="hint">
                    {{
                      $t(
                        "administration.copy.leave_these_empty_if_your_provider_supports_automatic",
                      )
                    }}
                  </p>
                  <label for="sso-client">{{
                    $t("administration.copy.application_client_id")
                  }}</label
                  ><UiInput
                    id="sso-client"
                    v-model.trim="clientId"
                    maxlength="2048"
                    autocomplete="off"
                  /><label for="sso-secret">{{
                    $t("administration.copy.application_client_secret")
                  }}</label
                  ><UiInput
                    id="sso-secret"
                    v-model="secret"
                    type="password"
                    maxlength="4096"
                    autocomplete="new-password"
                  /><label for="sso-registration">{{
                    $t(
                      "administration.copy.initial_registration_token_optional",
                    )
                  }}</label
                  ><UiInput
                    id="sso-registration"
                    v-model="registrationToken"
                    type="password"
                    maxlength="4096"
                    autocomplete="new-password"
                  /></details></template
              ><template v-else
                ><label for="sso-metadata">{{
                  $t("administration.copy.provider_configuration_metadata_xml")
                }}</label
                ><UiTextarea
                  id="sso-metadata"
                  v-model="metadata"
                  required
                  maxlength="131072"
                  rows="7"
                  spellcheck="false"
                />
                <p class="hint">
                  {{
                    $t(
                      "administration.copy.paste_the_metadata_document_supplied_by_your_it",
                    )
                  }}
                </p></template
              >
            </details>
            <div class="mt-5 flex gap-3">
              <UiButton
                type="button"
                variant="secondary"
                @click="providerStep = 1"
                >{{ $t("administration.copy.back") }}</UiButton
              ><UiButton
                type="button"
                :disabled="
                  protocol === 'oidc' ? !providerAddressValid : !metadata.trim()
                "
                @click="reviewConnection"
                variant="primary"
                >{{ $t("administration.copy.review_connection") }}</UiButton
              >
            </div>
          </div>
          <div v-else>
            <h3>{{ $t("administration.copy.ready_to_connect_your_team") }}</h3>
            <p>
              {{
                $t(
                  "administration.copy.we_will_verify_your_domain_and_connect_your",
                )
              }}
            </p>
            <dl class="my-5 rounded-xl bg-canvas p-4 text-sm">
              <dt class="text-muted">
                {{ $t("administration.copy.email_domain") }}
              </dt>
              <dd class="mt-1 mb-4 font-medium text-foreground">
                {{ domain }}
              </dd>
              <dt class="text-muted">
                {{ $t("administration.copy.sign_in_provider") }}
              </dt>
              <dd class="mt-1 break-all text-foreground">
                {{
                  protocol === "oidc"
                    ? issuer
                    : $t("administration.copy.saml_provider_configuration")
                }}
              </dd>
            </dl>
            <label class="check"
              ><UiCheckbox v-model="jit" type="checkbox" />{{
                $t(
                  "administration.copy.automatically_add_new_colleagues_on_first_sign_in",
                )
              }}</label
            >
            <p class="hint">
              {{
                $t(
                  "administration.copy.off_by_default_new_people_receive_no_permissions",
                )
              }}
            </p>
            <div class="mt-5 flex gap-3">
              <UiButton
                type="button"
                variant="secondary"
                @click="providerStep = 2"
                >{{ $t("administration.copy.back") }}</UiButton
              ><UiButton type="submit" variant="primary">{{
                $t("administration.copy.verify_and_connect")
              }}</UiButton>
            </div>
          </div>
        </fieldset>
      </form>
    </UiCard>
  </template>
</template>
