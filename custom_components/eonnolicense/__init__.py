"""Inițializarea integrării E·ON România."""

import logging
from dataclasses import dataclass, field

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.update_coordinator import UpdateFailed

from .const import DOMAIN, DEFAULT_UPDATE_INTERVAL, DOMAIN_TOKEN_STORE, PLATFORMS
from .api import EonApiClient
from .coordinator import EonRomaniaCoordinator

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class EonRomaniaRuntimeData:
    """Structură tipizată pentru datele runtime ale integrării."""

    coordinators: dict[str, EonRomaniaCoordinator] = field(default_factory=dict)
    api_client: EonApiClient | None = None


async def async_setup(hass: HomeAssistant, config: dict):
    """Configurează integrarea globală E·ON România."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Configurează integrarea pentru o anumită intrare (config entry)."""
    _LOGGER.info("Se configurează integrarea %s (entry_id=%s).", DOMAIN, entry.entry_id)

    hass.data.setdefault(DOMAIN, {})


    session = async_get_clientsession(hass)
    username = entry.data["username"]
    password = entry.data["password"]
    update_interval = entry.data.get("update_interval", DEFAULT_UPDATE_INTERVAL)

    # Compatibilitate: formatul vechi (un singur cod_incasare) vs nou (listă)
    selected_contracts = entry.data.get("selected_contracts", [])
    if not selected_contracts:
        # Formatul vechi — un singur contract
        old_cod = entry.data.get("cod_incasare", "")
        if old_cod:
            selected_contracts = [old_cod]

    is_account_only = entry.data.get("account_only", False) or not selected_contracts

    if not selected_contracts and not is_account_only:
        _LOGGER.error(
            "Nu există contracte selectate pentru %s (entry_id=%s).",
            DOMAIN, entry.entry_id,
        )
        return False

    _LOGGER.debug(
        "Contracte selectate pentru %s (entry_id=%s): %s, interval=%ss, account_only=%s.",
        DOMAIN, entry.entry_id, selected_contracts, update_interval, is_account_only,
    )

    # Un singur client API partajat (un singur cont, un singur token)
    api_client = EonApiClient(session, username, password)

    # Injectăm token-ul salvat — prioritate: hass.data (proaspăt, de la config_flow),
    # apoi config_entry.data (persistent, pentru restart HA)
    token_store = hass.data.get(DOMAIN_TOKEN_STORE, {})
    stored_token = token_store.pop(username.lower(), None)
    if stored_token:
        api_client.inject_token(stored_token)
        _LOGGER.debug(
            "Token injectat din config_flow (proaspăt) pentru %s (entry_id=%s).",
            username, entry.entry_id,
        )
        # Ștergem notificarea de re-autentificare (dacă există)
        for contract in selected_contracts:
            persistent_notification.async_dismiss(
                hass, f"eonromania_reauth_{contract}"
            )
    elif entry.data.get("token_data"):
        api_client.inject_token(entry.data["token_data"])
        _LOGGER.debug(
            "Token injectat din config_entry.data (persistent) pentru %s (entry_id=%s).",
            username, entry.entry_id,
        )
    else:
        _LOGGER.debug(
            "Niciun token salvat disponibil pentru %s (entry_id=%s). Se va face login.",
            username, entry.entry_id,
        )
    # Curățăm store-ul dacă e gol
    if DOMAIN_TOKEN_STORE in hass.data and not hass.data[DOMAIN_TOKEN_STORE]:
        hass.data.pop(DOMAIN_TOKEN_STORE, None)

    # Metadatele contractelor (tip utilitate, colectiv/nu)
    contract_metadata = entry.data.get("contract_metadata", {})

    # Creăm câte un coordinator per contract selectat
    coordinators: dict[str, EonRomaniaCoordinator] = {}

    if is_account_only:
        # Cont fără contracte — un singur coordinator pentru date personale
        coordinator = EonRomaniaCoordinator(
            hass,
            api_client=api_client,
            cod_incasare="__account__",
            update_interval=update_interval,
            is_collective=False,
            config_entry=entry,
            account_only=True,
        )

        try:
            await coordinator.async_config_entry_first_refresh()
        except UpdateFailed as err:
            _LOGGER.error(
                "Prima actualizare eșuată pentru date personale (entry_id=%s): %s",
                entry.entry_id, err,
            )
            return False
        except Exception as err:
            _LOGGER.exception(
                "Eroare neașteptată la date personale (entry_id=%s): %s",
                entry.entry_id, err,
            )
            return False

        coordinators["__account__"] = coordinator
    else:
        for cod in selected_contracts:
            meta = contract_metadata.get(cod, {})
            is_collective = meta.get("is_collective", False)

            coordinator = EonRomaniaCoordinator(
                hass,
                api_client=api_client,
                cod_incasare=cod,
                update_interval=update_interval,
                is_collective=is_collective,
                config_entry=entry,
            )

            try:
                await coordinator.async_config_entry_first_refresh()
            except UpdateFailed as err:
                _LOGGER.error(
                    "Prima actualizare eșuată (entry_id=%s, contract=%s): %s",
                    entry.entry_id, cod, err,
                )
                # Continuăm cu restul contractelor — nu oprim totul pentru unul
                continue
            except Exception as err:
                _LOGGER.exception(
                    "Eroare neașteptată la prima actualizare (entry_id=%s, contract=%s): %s",
                    entry.entry_id, cod, err,
                )
                continue

            coordinators[cod] = coordinator

    if not coordinators:
        _LOGGER.error(
            "Niciun coordinator inițializat cu succes pentru %s (entry_id=%s).",
            DOMAIN, entry.entry_id,
        )
        return False

    _LOGGER.info(
        "%s coordinatoare active din %s contracte selectate (entry_id=%s, account_only=%s).",
        len(coordinators), len(selected_contracts), entry.entry_id, is_account_only,
    )

    # Salvăm datele runtime
    entry.runtime_data = EonRomaniaRuntimeData(
        coordinators=coordinators,
        api_client=api_client,
    )

    # Încărcăm platformele
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Listener pentru modificarea opțiunilor
    entry.async_on_unload(entry.add_update_listener(_async_update_options))

    _LOGGER.info(
        "Integrarea %s configurată (entry_id=%s, contracte=%s).",
        DOMAIN, entry.entry_id, list(coordinators.keys()),
    )
    return True


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry):
    """Reîncarcă integrarea când opțiunile se schimbă."""
    _LOGGER.info(
        "Opțiunile integrării %s s-au schimbat (entry_id=%s). Se reîncarcă...",
        DOMAIN, entry.entry_id,
    )
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Descărcarea intrării din config_entries."""
    _LOGGER.info(
        "[EonRomania] ── async_unload_entry ── entry_id=%s",
        entry.entry_id,
    )

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    _LOGGER.debug("[EonRomania] Unload platforme: %s", "OK" if unload_ok else "EȘUAT")

    if unload_ok:
        # runtime_data se curăță automat de HA la unload — nu mai facem pop manual

        # Verifică dacă mai sunt entry-uri active (BUG-03: folosim config_entries, nu hass.data)
        remaining_entries = hass.config_entries.async_entries(DOMAIN)
        # Excludem entry-ul curent (tocmai descărcat)
        entry_ids_ramase = {e.entry_id for e in remaining_entries if e.entry_id != entry.entry_id}

        _LOGGER.debug(
            "[EonRomania] Entry-uri rămase după unload: %d (%s)",
            len(entry_ids_ramase),
            entry_ids_ramase or "niciuna",
        )

        if not entry_ids_ramase:
            _LOGGER.info("[EonRomania] Ultima entry descărcată — curăț domeniul complet")

            # Elimină domeniul complet
            hass.data.pop(DOMAIN, None)
            _LOGGER.debug("[EonRomania] hass.data[%s] eliminat complet", DOMAIN)

            _LOGGER.info("[EonRomania] Cleanup complet — domeniul %s descărcat", DOMAIN)
    else:
        _LOGGER.error("[EonRomania] Unload EȘUAT pentru entry_id=%s", entry.entry_id)

    return unload_ok


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrare de la versiuni vechi la versiunea curentă."""
    _LOGGER.debug(
        "Migrare config entry %s de la versiunea %s.",
        config_entry.entry_id, config_entry.version,
    )

    if config_entry.version < 3:
        # v1/v2 → v3: convertim cod_incasare la selected_contracts[]
        old_data = dict(config_entry.data)
        old_cod = old_data.get("cod_incasare", "")
        old_interval = old_data.get("update_interval",
                        config_entry.options.get("update_interval", DEFAULT_UPDATE_INTERVAL))

        new_data = {
            "username": old_data.get("username", ""),
            "password": old_data.get("password", ""),
            "update_interval": old_interval,
            "select_all": False,
            "selected_contracts": [old_cod] if old_cod else [],
        }
        # BUG-04: Păstrează token_data la migrare (evită re-autentificare cu MFA)
        if old_data.get("token_data"):
            new_data["token_data"] = old_data["token_data"]

        _LOGGER.info(
            "Migrare entry %s: v%s → v3 (cod_incasare=%s → selected_contracts).",
            config_entry.entry_id, config_entry.version, old_cod,
        )

        hass.config_entries.async_update_entry(
            config_entry, data=new_data, options={}, version=3
        )
        return True

    _LOGGER.error(
        "Versiune necunoscută pentru migrare: %s (entry_id=%s).",
        config_entry.version, config_entry.entry_id,
    )
    return False
