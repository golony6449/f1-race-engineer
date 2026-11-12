"""F1 25 UDP 구조체 정의(packetFormat 2025).

원본: EA 공식 "F1 25 Telemetry Output Structures.txt".
필드 순서·형·배열 길이를 원문 그대로 옮겼다. 원문 사본은 저작권 때문에
레포에 넣지 않는다(`docs/spec/README.md` 참고).

필드 이름은 원문 이름(`m_...`)을 그대로 적고 `schema.snake_case`가 body 키로
바꾼다. 공식 문서의 패킷별 전체 바이트 수는 `spec.PACKET_SIZES`에 있고,
`tests/udp/test_spec.py`가 이 정의들의 크기 합과 대조한다.
"""

from __future__ import annotations

from f1re.udp.schema import (
    EventDetails,
    PacketDef,
    StructDef,
    F,
    FA,
    M,
    RAW,
    S,
    T,
    TA,
)
# 형식 모듈은 FORMAT, PACKET_SIZES, PACKETS를 제공한다(spec.FORMATS 규약).
from f1re.udp.spec import PACKET_SIZES, PacketId  # noqa: F401

FORMAT = 2025

# 공식 원문 상수.
MAX_NUM_CARS = 22
MAX_PARTICIPANT_NAME_LEN = 32
MAX_TYRE_STINTS = 8
MAX_NUM_TYRE_SETS = 13 + 7  # 13 slick + 7 wet
MAX_MARSHAL_ZONES_PER_LAP = 21
MAX_WEATHER_FORECAST_SAMPLES = 64
MAX_SESSIONS_IN_WEEKEND = 12
MAX_NUM_LAPS_IN_HISTORY = 100
MAX_NUM_LAPS_IN_LAP_POSITIONS_HISTORY = 50
EVENT_STRING_CODE_LEN = 4
EVENT_DETAILS_SIZE = 12  # union에서 가장 큰 멤버(SpeedTrap) 크기


# ---------------------------------------------------------------------------
# Motion - 1349 bytes
# ---------------------------------------------------------------------------
CAR_MOTION_DATA = StructDef(
    (
        F("m_worldPositionX", "f"),
        F("m_worldPositionY", "f"),
        F("m_worldPositionZ", "f"),
        F("m_worldVelocityX", "f"),
        F("m_worldVelocityY", "f"),
        F("m_worldVelocityZ", "f"),
        F("m_worldForwardDirX", "h"),
        F("m_worldForwardDirY", "h"),
        F("m_worldForwardDirZ", "h"),
        F("m_worldRightDirX", "h"),
        F("m_worldRightDirY", "h"),
        F("m_worldRightDirZ", "h"),
        F("m_gForceLateral", "f"),
        F("m_gForceLongitudinal", "f"),
        F("m_gForceVertical", "f"),
        F("m_yaw", "f"),
        F("m_pitch", "f"),
        F("m_roll", "f"),
    )
)


# ---------------------------------------------------------------------------
# Session - 753 bytes
# ---------------------------------------------------------------------------
MARSHAL_ZONE = StructDef(
    (
        F("m_zoneStart", "f"),
        F("m_zoneFlag", "b"),
    )
)

WEATHER_FORECAST_SAMPLE = StructDef(
    (
        F("m_sessionType", "B"),
        F("m_timeOffset", "B"),
        F("m_weather", "B"),
        F("m_trackTemperature", "b"),
        F("m_trackTemperatureChange", "b"),
        F("m_airTemperature", "b"),
        F("m_airTemperatureChange", "b"),
        F("m_rainPercentage", "B"),
    )
)


# ---------------------------------------------------------------------------
# Lap - 1285 bytes
# ---------------------------------------------------------------------------
LAP_DATA = StructDef(
    (
        F("m_lastLapTimeInMS", "I"),
        F("m_currentLapTimeInMS", "I"),
        F("m_sector1TimeMSPart", "H"),
        F("m_sector1TimeMinutesPart", "B"),
        F("m_sector2TimeMSPart", "H"),
        F("m_sector2TimeMinutesPart", "B"),
        F("m_deltaToCarInFrontMSPart", "H"),
        F("m_deltaToCarInFrontMinutesPart", "B"),
        F("m_deltaToRaceLeaderMSPart", "H"),
        F("m_deltaToRaceLeaderMinutesPart", "B"),
        F("m_lapDistance", "f"),
        F("m_totalDistance", "f"),
        F("m_safetyCarDelta", "f"),
        F("m_carPosition", "B"),
        F("m_currentLapNum", "B"),
        F("m_pitStatus", "B"),
        F("m_numPitStops", "B"),
        F("m_sector", "B"),
        F("m_currentLapInvalid", "B"),
        F("m_penalties", "B"),
        F("m_totalWarnings", "B"),
        F("m_cornerCuttingWarnings", "B"),
        F("m_numUnservedDriveThroughPens", "B"),
        F("m_numUnservedStopGoPens", "B"),
        F("m_gridPosition", "B"),
        F("m_driverStatus", "B"),
        F("m_resultStatus", "B"),
        F("m_pitLaneTimerActive", "B"),
        F("m_pitLaneTimeInLaneInMS", "H"),
        F("m_pitStopTimerInMS", "H"),
        F("m_pitStopShouldServePen", "B"),
        F("m_speedTrapFastestSpeed", "f"),
        F("m_speedTrapFastestLap", "B"),
    )
)


# ---------------------------------------------------------------------------
# Event - 45 bytes
# ---------------------------------------------------------------------------
FASTEST_LAP = StructDef(
    (
        F("vehicleIdx", "B"),
        F("lapTime", "f"),
    )
)

RETIREMENT = StructDef(
    (
        F("vehicleIdx", "B"),
        F("reason", "B"),
    )
)

DRS_DISABLED = StructDef((F("reason", "B"),))

TEAM_MATE_IN_PITS = StructDef((F("vehicleIdx", "B"),))

RACE_WINNER = StructDef((F("vehicleIdx", "B"),))

PENALTY = StructDef(
    (
        F("penaltyType", "B"),
        F("infringementType", "B"),
        F("vehicleIdx", "B"),
        F("otherVehicleIdx", "B"),
        F("time", "B"),
        F("lapNum", "B"),
        F("placesGained", "B"),
    )
)

SPEED_TRAP = StructDef(
    (
        F("vehicleIdx", "B"),
        F("speed", "f"),
        F("isOverallFastestInSession", "B"),
        F("isDriverFastestInSession", "B"),
        F("fastestVehicleIdxInSession", "B"),
        F("fastestSpeedInSession", "f"),
    )
)

START_LIGHTS = StructDef((F("numLights", "B"),))

DRIVE_THROUGH_PENALTY_SERVED = StructDef((F("vehicleIdx", "B"),))

STOP_GO_PENALTY_SERVED = StructDef(
    (
        F("vehicleIdx", "B"),
        F("stopTime", "f"),
    )
)

FLASHBACK = StructDef(
    (
        F("flashbackFrameIdentifier", "I"),
        F("flashbackSessionTime", "f"),
    )
)

BUTTONS = StructDef((F("buttonStatus", "I"),))

OVERTAKE = StructDef(
    (
        F("overtakingVehicleIdx", "B"),
        F("beingOvertakenVehicleIdx", "B"),
    )
)

SAFETY_CAR = StructDef(
    (
        F("safetyCarType", "B"),
        F("eventType", "B"),
    )
)

COLLISION = StructDef(
    (
        F("vehicle1Idx", "B"),
        F("vehicle2Idx", "B"),
    )
)

EVENT_DETAILS = EventDetails(
    size=EVENT_DETAILS_SIZE,
    structs={
        "FTLP": FASTEST_LAP,
        "RTMT": RETIREMENT,
        "DRSD": DRS_DISABLED,
        "TMPT": TEAM_MATE_IN_PITS,
        "RCWN": RACE_WINNER,
        "PENA": PENALTY,
        "SPTP": SPEED_TRAP,
        "STLG": START_LIGHTS,
        "DTSV": DRIVE_THROUGH_PENALTY_SERVED,
        "SGSV": STOP_GO_PENALTY_SERVED,
        "FLBK": FLASHBACK,
        "BUTN": BUTTONS,
        "OVTK": OVERTAKE,
        "SCAR": SAFETY_CAR,
        "COLL": COLLISION,
    },
    empty_codes=frozenset({"SSTA", "SEND", "DRSE", "CHQF", "LGOT", "RDFL"}),
)


# ---------------------------------------------------------------------------
# Participants - 1284 bytes
# ---------------------------------------------------------------------------
LIVERY_COLOUR = StructDef(
    (
        F("red", "B"),
        F("green", "B"),
        F("blue", "B"),
    )
)

PARTICIPANT_DATA = StructDef(
    (
        F("m_aiControlled", "B"),
        F("m_driverId", "B"),
        F("m_networkId", "B"),
        F("m_teamId", "B"),
        F("m_myTeam", "B"),
        F("m_raceNumber", "B"),
        F("m_nationality", "B"),
        S("m_name", MAX_PARTICIPANT_NAME_LEN),
        F("m_yourTelemetry", "B"),
        F("m_showOnlineNames", "B"),
        F("m_techLevel", "H"),
        F("m_platform", "B"),
        F("m_numColours", "B"),
        TA("m_liveryColours", LIVERY_COLOUR, 4),
    )
)


# ---------------------------------------------------------------------------
# Car Setups - 1133 bytes
# ---------------------------------------------------------------------------
CAR_SETUP_DATA = StructDef(
    (
        F("m_frontWing", "B"),
        F("m_rearWing", "B"),
        F("m_onThrottle", "B"),
        F("m_offThrottle", "B"),
        F("m_frontCamber", "f"),
        F("m_rearCamber", "f"),
        F("m_frontToe", "f"),
        F("m_rearToe", "f"),
        F("m_frontSuspension", "B"),
        F("m_rearSuspension", "B"),
        F("m_frontAntiRollBar", "B"),
        F("m_rearAntiRollBar", "B"),
        F("m_frontSuspensionHeight", "B"),
        F("m_rearSuspensionHeight", "B"),
        F("m_brakePressure", "B"),
        F("m_brakeBias", "B"),
        F("m_engineBraking", "B"),
        F("m_rearLeftTyrePressure", "f"),
        F("m_rearRightTyrePressure", "f"),
        F("m_frontLeftTyrePressure", "f"),
        F("m_frontRightTyrePressure", "f"),
        F("m_ballast", "B"),
        F("m_fuelLoad", "f"),
    )
)


# ---------------------------------------------------------------------------
# Car Telemetry - 1352 bytes
# ---------------------------------------------------------------------------
CAR_TELEMETRY_DATA = StructDef(
    (
        F("m_speed", "H"),
        F("m_throttle", "f"),
        F("m_steer", "f"),
        F("m_brake", "f"),
        F("m_clutch", "B"),
        F("m_gear", "b"),
        F("m_engineRPM", "H"),
        F("m_drs", "B"),
        F("m_revLightsPercent", "B"),
        F("m_revLightsBitValue", "H"),
        FA("m_brakesTemperature", "H", 4),
        FA("m_tyresSurfaceTemperature", "B", 4),
        FA("m_tyresInnerTemperature", "B", 4),
        F("m_engineTemperature", "H"),
        FA("m_tyresPressure", "f", 4),
        FA("m_surfaceType", "B", 4),
    )
)


# ---------------------------------------------------------------------------
# Car Status - 1239 bytes
# ---------------------------------------------------------------------------
CAR_STATUS_DATA = StructDef(
    (
        F("m_tractionControl", "B"),
        F("m_antiLockBrakes", "B"),
        F("m_fuelMix", "B"),
        F("m_frontBrakeBias", "B"),
        F("m_pitLimiterStatus", "B"),
        F("m_fuelInTank", "f"),
        F("m_fuelCapacity", "f"),
        F("m_fuelRemainingLaps", "f"),
        F("m_maxRPM", "H"),
        F("m_idleRPM", "H"),
        F("m_maxGears", "B"),
        F("m_drsAllowed", "B"),
        F("m_drsActivationDistance", "H"),
        F("m_actualTyreCompound", "B"),
        F("m_visualTyreCompound", "B"),
        F("m_tyresAgeLaps", "B"),
        F("m_vehicleFIAFlags", "b"),
        F("m_enginePowerICE", "f"),
        F("m_enginePowerMGUK", "f"),
        F("m_ersStoreEnergy", "f"),
        F("m_ersDeployMode", "B"),
        F("m_ersHarvestedThisLapMGUK", "f"),
        F("m_ersHarvestedThisLapMGUH", "f"),
        F("m_ersDeployedThisLap", "f"),
        F("m_networkPaused", "B"),
    )
)


# ---------------------------------------------------------------------------
# Final Classification - 1042 bytes
# ---------------------------------------------------------------------------
FINAL_CLASSIFICATION_DATA = StructDef(
    (
        F("m_position", "B"),
        F("m_numLaps", "B"),
        F("m_gridPosition", "B"),
        F("m_points", "B"),
        F("m_numPitStops", "B"),
        F("m_resultStatus", "B"),
        F("m_resultReason", "B"),
        F("m_bestLapTimeInMS", "I"),
        F("m_totalRaceTime", "d"),
        F("m_penaltiesTime", "B"),
        F("m_numPenalties", "B"),
        F("m_numTyreStints", "B"),
        FA("m_tyreStintsActual", "B", MAX_TYRE_STINTS),
        FA("m_tyreStintsVisual", "B", MAX_TYRE_STINTS),
        FA("m_tyreStintsEndLaps", "B", MAX_TYRE_STINTS),
    )
)


# ---------------------------------------------------------------------------
# Lobby Info - 954 bytes
# ---------------------------------------------------------------------------
LOBBY_INFO_DATA = StructDef(
    (
        F("m_aiControlled", "B"),
        F("m_teamId", "B"),
        F("m_nationality", "B"),
        F("m_platform", "B"),
        S("m_name", MAX_PARTICIPANT_NAME_LEN),
        F("m_carNumber", "B"),
        F("m_yourTelemetry", "B"),
        F("m_showOnlineNames", "B"),
        F("m_techLevel", "H"),
        F("m_readyStatus", "B"),
    )
)


# ---------------------------------------------------------------------------
# Car Damage - 1041 bytes
# ---------------------------------------------------------------------------
CAR_DAMAGE_DATA = StructDef(
    (
        FA("m_tyresWear", "f", 4),
        FA("m_tyresDamage", "B", 4),
        FA("m_brakesDamage", "B", 4),
        FA("m_tyreBlisters", "B", 4),
        F("m_frontLeftWingDamage", "B"),
        F("m_frontRightWingDamage", "B"),
        F("m_rearWingDamage", "B"),
        F("m_floorDamage", "B"),
        F("m_diffuserDamage", "B"),
        F("m_sidepodDamage", "B"),
        F("m_drsFault", "B"),
        F("m_ersFault", "B"),
        F("m_gearBoxDamage", "B"),
        F("m_engineDamage", "B"),
        F("m_engineMGUHWear", "B"),
        F("m_engineESWear", "B"),
        F("m_engineCEWear", "B"),
        F("m_engineICEWear", "B"),
        F("m_engineMGUKWear", "B"),
        F("m_engineTCWear", "B"),
        F("m_engineBlown", "B"),
        F("m_engineSeized", "B"),
    )
)


# ---------------------------------------------------------------------------
# Session History - 1460 bytes
# ---------------------------------------------------------------------------
LAP_HISTORY_DATA = StructDef(
    (
        F("m_lapTimeInMS", "I"),
        F("m_sector1TimeMSPart", "H"),
        F("m_sector1TimeMinutesPart", "B"),
        F("m_sector2TimeMSPart", "H"),
        F("m_sector2TimeMinutesPart", "B"),
        F("m_sector3TimeMSPart", "H"),
        F("m_sector3TimeMinutesPart", "B"),
        F("m_lapValidBitFlags", "B"),
    )
)

TYRE_STINT_HISTORY_DATA = StructDef(
    (
        F("m_endLap", "B"),
        F("m_tyreActualCompound", "B"),
        F("m_tyreVisualCompound", "B"),
    )
)


# ---------------------------------------------------------------------------
# Tyre Sets - 231 bytes
# ---------------------------------------------------------------------------
TYRE_SET_DATA = StructDef(
    (
        F("m_actualTyreCompound", "B"),
        F("m_visualTyreCompound", "B"),
        F("m_wear", "B"),
        F("m_available", "B"),
        F("m_recommendedSession", "B"),
        F("m_lifeSpan", "B"),
        F("m_usableLife", "B"),
        F("m_lapDeltaTime", "h"),
        F("m_fitted", "B"),
    )
)


# ---------------------------------------------------------------------------
# Motion Ex - 273 bytes
# ---------------------------------------------------------------------------
MOTION_EX_BODY = StructDef(
    (
        FA("m_suspensionPosition", "f", 4),
        FA("m_suspensionVelocity", "f", 4),
        FA("m_suspensionAcceleration", "f", 4),
        FA("m_wheelSpeed", "f", 4),
        FA("m_wheelSlipRatio", "f", 4),
        FA("m_wheelSlipAngle", "f", 4),
        FA("m_wheelLatForce", "f", 4),
        FA("m_wheelLongForce", "f", 4),
        F("m_heightOfCOGAboveGround", "f"),
        F("m_localVelocityX", "f"),
        F("m_localVelocityY", "f"),
        F("m_localVelocityZ", "f"),
        F("m_angularVelocityX", "f"),
        F("m_angularVelocityY", "f"),
        F("m_angularVelocityZ", "f"),
        F("m_angularAccelerationX", "f"),
        F("m_angularAccelerationY", "f"),
        F("m_angularAccelerationZ", "f"),
        F("m_frontWheelsAngle", "f"),
        FA("m_wheelVertForce", "f", 4),
        F("m_frontAeroHeight", "f"),
        F("m_rearAeroHeight", "f"),
        F("m_frontRollAngle", "f"),
        F("m_rearRollAngle", "f"),
        F("m_chassisYaw", "f"),
        F("m_chassisPitch", "f"),
        FA("m_wheelCamber", "f", 4),
        FA("m_wheelCamberGain", "f", 4),
    )
)


# ---------------------------------------------------------------------------
# Time Trial - 101 bytes
# ---------------------------------------------------------------------------
TIME_TRIAL_DATA_SET = StructDef(
    (
        F("m_carIdx", "B"),
        F("m_teamId", "B"),
        F("m_lapTimeInMS", "I"),
        F("m_sector1TimeInMS", "I"),
        F("m_sector2TimeInMS", "I"),
        F("m_sector3TimeInMS", "I"),
        F("m_tractionControl", "B"),
        F("m_gearboxAssist", "B"),
        F("m_antiLockBrakes", "B"),
        F("m_equalCarPerformance", "B"),
        F("m_customSetup", "B"),
        F("m_valid", "B"),
    )
)


# ---------------------------------------------------------------------------
# 패킷 본문과 ID 표
# ---------------------------------------------------------------------------
PACKETS: dict[PacketId, PacketDef] = {
    PacketId.MOTION: PacketDef(
        PacketId.MOTION,
        "motion",
        StructDef((TA("m_carMotionData", CAR_MOTION_DATA, MAX_NUM_CARS),)),
    ),
    PacketId.SESSION: PacketDef(
        PacketId.SESSION,
        "session",
        StructDef(
            (
                F("m_weather", "B"),
                F("m_trackTemperature", "b"),
                F("m_airTemperature", "b"),
                F("m_totalLaps", "B"),
                F("m_trackLength", "H"),
                F("m_sessionType", "B"),
                F("m_trackId", "b"),
                F("m_formula", "B"),
                F("m_sessionTimeLeft", "H"),
                F("m_sessionDuration", "H"),
                F("m_pitSpeedLimit", "B"),
                F("m_gamePaused", "B"),
                F("m_isSpectating", "B"),
                F("m_spectatorCarIndex", "B"),
                F("m_sliProNativeSupport", "B"),
                F("m_numMarshalZones", "B"),
                TA("m_marshalZones", MARSHAL_ZONE, MAX_MARSHAL_ZONES_PER_LAP),
                F("m_safetyCarStatus", "B"),
                F("m_networkGame", "B"),
                F("m_numWeatherForecastSamples", "B"),
                TA(
                    "m_weatherForecastSamples",
                    WEATHER_FORECAST_SAMPLE,
                    MAX_WEATHER_FORECAST_SAMPLES,
                ),
                F("m_forecastAccuracy", "B"),
                F("m_aiDifficulty", "B"),
                F("m_seasonLinkIdentifier", "I"),
                F("m_weekendLinkIdentifier", "I"),
                F("m_sessionLinkIdentifier", "I"),
                F("m_pitStopWindowIdealLap", "B"),
                F("m_pitStopWindowLatestLap", "B"),
                F("m_pitStopRejoinPosition", "B"),
                F("m_steeringAssist", "B"),
                F("m_brakingAssist", "B"),
                F("m_gearboxAssist", "B"),
                F("m_pitAssist", "B"),
                F("m_pitReleaseAssist", "B"),
                F("m_ERSAssist", "B"),
                F("m_DRSAssist", "B"),
                F("m_dynamicRacingLine", "B"),
                F("m_dynamicRacingLineType", "B"),
                F("m_gameMode", "B"),
                F("m_ruleSet", "B"),
                F("m_timeOfDay", "I"),
                F("m_sessionLength", "B"),
                F("m_speedUnitsLeadPlayer", "B"),
                F("m_temperatureUnitsLeadPlayer", "B"),
                F("m_speedUnitsSecondaryPlayer", "B"),
                F("m_temperatureUnitsSecondaryPlayer", "B"),
                F("m_numSafetyCarPeriods", "B"),
                F("m_numVirtualSafetyCarPeriods", "B"),
                F("m_numRedFlagPeriods", "B"),
                F("m_equalCarPerformance", "B"),
                F("m_recoveryMode", "B"),
                F("m_flashbackLimit", "B"),
                F("m_surfaceType", "B"),
                F("m_lowFuelMode", "B"),
                F("m_raceStarts", "B"),
                F("m_tyreTemperature", "B"),
                F("m_pitLaneTyreSim", "B"),
                F("m_carDamage", "B"),
                F("m_carDamageRate", "B"),
                F("m_collisions", "B"),
                F("m_collisionsOffForFirstLapOnly", "B"),
                F("m_mpUnsafePitRelease", "B"),
                F("m_mpOffForGriefing", "B"),
                F("m_cornerCuttingStringency", "B"),
                F("m_parcFermeRules", "B"),
                F("m_pitStopExperience", "B"),
                F("m_safetyCar", "B"),
                F("m_safetyCarExperience", "B"),
                F("m_formationLap", "B"),
                F("m_formationLapExperience", "B"),
                F("m_redFlags", "B"),
                F("m_affectsLicenceLevelSolo", "B"),
                F("m_affectsLicenceLevelMP", "B"),
                F("m_numSessionsInWeekend", "B"),
                FA("m_weekendStructure", "B", MAX_SESSIONS_IN_WEEKEND),
                F("m_sector2LapDistanceStart", "f"),
                F("m_sector3LapDistanceStart", "f"),
            )
        ),
    ),
    PacketId.LAP_DATA: PacketDef(
        PacketId.LAP_DATA,
        "lap_data",
        StructDef(
            (
                TA("m_lapData", LAP_DATA, MAX_NUM_CARS),
                F("m_timeTrialPBCarIdx", "B"),
                F("m_timeTrialRivalCarIdx", "B"),
            )
        ),
    ),
    PacketId.EVENT: PacketDef(
        PacketId.EVENT,
        "event",
        StructDef(
            (
                # 원문은 uint8[4]로 선언하지만, 이벤트 코드 4글자를 문자열로 다룬다.
                S("m_eventStringCode", EVENT_STRING_CODE_LEN),
                RAW("m_eventDetails", EVENT_DETAILS_SIZE),
            )
        ),
        event=EVENT_DETAILS,
    ),
    PacketId.PARTICIPANTS: PacketDef(
        PacketId.PARTICIPANTS,
        "participants",
        StructDef(
            (
                F("m_numActiveCars", "B"),
                TA("m_participants", PARTICIPANT_DATA, MAX_NUM_CARS),
            )
        ),
    ),
    PacketId.CAR_SETUPS: PacketDef(
        PacketId.CAR_SETUPS,
        "car_setups",
        StructDef(
            (
                TA("m_carSetupData", CAR_SETUP_DATA, MAX_NUM_CARS),
                F("m_nextFrontWingValue", "f"),
            )
        ),
    ),
    PacketId.CAR_TELEMETRY: PacketDef(
        PacketId.CAR_TELEMETRY,
        "car_telemetry",
        StructDef(
            (
                TA("m_carTelemetryData", CAR_TELEMETRY_DATA, MAX_NUM_CARS),
                F("m_mfdPanelIndex", "B"),
                F("m_mfdPanelIndexSecondaryPlayer", "B"),
                F("m_suggestedGear", "b"),
            )
        ),
    ),
    PacketId.CAR_STATUS: PacketDef(
        PacketId.CAR_STATUS,
        "car_status",
        StructDef((TA("m_carStatusData", CAR_STATUS_DATA, MAX_NUM_CARS),)),
    ),
    PacketId.FINAL_CLASSIFICATION: PacketDef(
        PacketId.FINAL_CLASSIFICATION,
        "final_classification",
        StructDef(
            (
                F("m_numCars", "B"),
                TA("m_classificationData", FINAL_CLASSIFICATION_DATA, MAX_NUM_CARS),
            )
        ),
    ),
    PacketId.LOBBY_INFO: PacketDef(
        PacketId.LOBBY_INFO,
        "lobby_info",
        StructDef(
            (
                F("m_numPlayers", "B"),
                TA("m_lobbyPlayers", LOBBY_INFO_DATA, MAX_NUM_CARS),
            )
        ),
    ),
    PacketId.CAR_DAMAGE: PacketDef(
        PacketId.CAR_DAMAGE,
        "car_damage",
        StructDef((TA("m_carDamageData", CAR_DAMAGE_DATA, MAX_NUM_CARS),)),
    ),
    PacketId.SESSION_HISTORY: PacketDef(
        PacketId.SESSION_HISTORY,
        "session_history",
        StructDef(
            (
                F("m_carIdx", "B"),
                F("m_numLaps", "B"),
                F("m_numTyreStints", "B"),
                F("m_bestLapTimeLapNum", "B"),
                F("m_bestSector1LapNum", "B"),
                F("m_bestSector2LapNum", "B"),
                F("m_bestSector3LapNum", "B"),
                TA("m_lapHistoryData", LAP_HISTORY_DATA, MAX_NUM_LAPS_IN_HISTORY),
                TA("m_tyreStintsHistoryData", TYRE_STINT_HISTORY_DATA, MAX_TYRE_STINTS),
            )
        ),
    ),
    PacketId.TYRE_SETS: PacketDef(
        PacketId.TYRE_SETS,
        "tyre_sets",
        StructDef(
            (
                F("m_carIdx", "B"),
                TA("m_tyreSetData", TYRE_SET_DATA, MAX_NUM_TYRE_SETS),
                F("m_fittedIdx", "B"),
            )
        ),
    ),
    PacketId.MOTION_EX: PacketDef(
        PacketId.MOTION_EX,
        "motion_ex",
        MOTION_EX_BODY,
    ),
    PacketId.TIME_TRIAL: PacketDef(
        PacketId.TIME_TRIAL,
        "time_trial",
        StructDef(
            (
                T("m_playerSessionBestDataSet", TIME_TRIAL_DATA_SET),
                T("m_personalBestDataSet", TIME_TRIAL_DATA_SET),
                T("m_rivalDataSet", TIME_TRIAL_DATA_SET),
            )
        ),
    ),
    PacketId.LAP_POSITIONS: PacketDef(
        PacketId.LAP_POSITIONS,
        "lap_positions",
        StructDef(
            (
                F("m_numLaps", "B"),
                F("m_lapStart", "B"),
                M(
                    "m_positionForVehicleIdx",
                    "B",
                    MAX_NUM_LAPS_IN_LAP_POSITIONS_HISTORY,
                    MAX_NUM_CARS,
                ),
            )
        ),
    ),
}

# spec이 형식 표를 만들 때 참조하는 이름. 현재 정의가 모든 패킷 ID를 덮는지
# 모듈 로드 시점에 확인한다.
assert set(PACKETS) == set(PacketId), "정의되지 않은 패킷 ID가 있다"
