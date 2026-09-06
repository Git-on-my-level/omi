import Darwin
import Foundation

extension AgentRuntimeProcess {
  /// The projection contains prompt bytes, so QA capture refuses to run unless
  /// the exact bundle-scoped runtime state is owner-only. This is a read-only
  /// preflight; normal app state is never chmod'd by the producer.
  static func hasPrivateJITQAStateDirectory(
    bundleIdentifier: String? = Bundle.main.bundleIdentifier,
    stateDirectory: URL? = nil,
    attributesProvider: ((String) -> [FileAttributeKey: Any]?)? = nil
  ) -> Bool {
    guard bundleIdentifier == JITProactivitySourceProjection.qaBundleIdentifier else { return false }
    let directory =
      stateDirectory
      ?? URL(fileURLWithPath: defaultStateDirectory(bundleIdentifier: bundleIdentifier))
    let fileManager = FileManager.default
    guard pathHasNoSymbolicLinkComponent(directory.path),
      let directoryAttributes = attributes(
        atPath: directory.path, provider: attributesProvider),
      directoryAttributes[.type] as? FileAttributeType == .typeDirectory,
      isOwnedByCurrentUser(directoryAttributes),
      ((directoryAttributes[.posixPermissions] as? NSNumber)?.intValue ?? 0) & 0o777 == 0o700
    else { return false }

    let databaseURL = directory.appendingPathComponent("omi-agentd.sqlite3")
    let databasePaths = [
      databaseURL.path,
      databaseURL.path + "-wal",
      databaseURL.path + "-shm",
    ]
    guard fileManager.fileExists(atPath: databaseURL.path) else { return false }
    return databasePaths.allSatisfy { path in
      guard pathHasNoSymbolicLinkComponent(path) else { return false }
      guard let attributes = attributes(atPath: path, provider: attributesProvider),
        attributes[.type] as? FileAttributeType == .typeRegular
      else {
        // SQLite only creates WAL/SHM files while a write is active. Missing
        // sidecars are therefore private by construction.
        return path != databaseURL.path && !fileManager.fileExists(atPath: path)
      }
      return isOwnedByCurrentUser(attributes)
        && ((attributes[.posixPermissions] as? NSNumber)?.intValue ?? 0) & 0o777 == 0o600
    }
  }

  private static func attributes(
    atPath path: String,
    provider: ((String) -> [FileAttributeKey: Any]?)?
  ) -> [FileAttributeKey: Any]? {
    if let provider { return provider(path) }
    return try? FileManager.default.attributesOfItem(atPath: path)
  }

  private static func isOwnedByCurrentUser(_ attributes: [FileAttributeKey: Any]) -> Bool {
    guard let ownerAccountID = attributes[.ownerAccountID] as? NSNumber else { return false }
    return ownerAccountID.uint32Value == getuid()
  }

  private static func pathHasNoSymbolicLinkComponent(_ path: String) -> Bool {
    let fileManager = FileManager.default
    var current = URL(fileURLWithPath: path).standardizedFileURL
    while current.path != "/" {
      if (try? fileManager.destinationOfSymbolicLink(atPath: current.path)) != nil {
        return false
      }
      current.deleteLastPathComponent()
    }
    return true
  }
}

/// Private, qualification-only prompt materialization carried with the exact
/// agent run that admitted the JIT context snapshot. Prompt bytes stay in the
/// owner-scoped agent database; the runtime adds the snapshot hash when it
/// builds the durable run input.
struct JITProactivitySourceProjection: Equatable, Sendable {
  static let schemaVersion = "omi.jit.proactivity.source_projection.v1"
  static let qaBundleIdentifier = "com.omi.omi-jit-qa"
  static let qaOwnerID = "vi7SA9ckQCe4ccobWNxlbdcNdC23"
  /// The legacy projection intentionally covers the director prompt builders
  /// only. Retrieval, workstream pooling, and proactive-candidate short-circuits
  /// are recorded as disabled rather than represented by a hand-built prompt.
  static let legacyProjectionMode = "director_baseline_v1"

  let executionID: String
  let producerLane: JITProactivityLane
  let evaluationTime: String
  let timezone: String
  let contextID: String
  let legacyPrompt: String
  let legacyUncachedPrompt: String
  let nanoPrompt: String
  let fullPrompt: String

  /// Builds a projection only for the fixed QA bundle and owner. The exact
  /// budget and temporal tuple are copied from the admitted execution so a
  /// replay cannot be assembled from an operator-selected clock or context.
  static func makeIfPermitted(
    execution: JITPlannedExecution,
    ownerID: String,
    contextID: String,
    legacyPrompt: String,
    legacyUncachedPrompt: String,
    nanoPrompt: String,
    fullPrompt: String,
    bundleIdentifier: String? = Bundle.main.bundleIdentifier
  ) -> Self? {
    guard bundleIdentifier == qaBundleIdentifier,
      ownerID == qaOwnerID,
      let budget = execution.agentBudget,
      budget.contractVersion == JITProactivityAgentBudget.cloudQAContractVersion,
      !contextID.isEmpty,
      !legacyPrompt.isEmpty,
      !legacyUncachedPrompt.isEmpty,
      !nanoPrompt.isEmpty,
      !fullPrompt.isEmpty,
      let temporal = execution.temporalContext,
      let evaluatedAt = temporal.evaluatedAt,
      let timezone = temporal.timezoneIdentifier,
      temporal.timeZone != nil
    else { return nil }

    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    formatter.timeZone = temporal.timeZone
    return Self(
      executionID: budget.executionID,
      producerLane: execution.lane,
      evaluationTime: formatter.string(from: evaluatedAt),
      timezone: timezone,
      contextID: contextID,
      legacyPrompt: legacyPrompt,
      legacyUncachedPrompt: legacyUncachedPrompt,
      nanoPrompt: nanoPrompt,
      fullPrompt: fullPrompt)
  }

  /// The dictionary is restricted to JSON values and is sent only through
  /// the QA JIT request. The agent runtime adds `evidence_sha256` and the
  /// matching hash after it has built the admitted context snapshot.
  var wireDictionary: [String: Any] {
    [
      "schema_version": Self.schemaVersion,
      "owner_id": Self.qaOwnerID,
      "execution_id": executionID,
      "producer_lane": producerLane.rawValue,
      "matched_input": [
        "evaluation_time": evaluationTime,
        "timezone": timezone,
        "context_id": contextID,
      ],
      "legacy": [
        "prompt": legacyPrompt,
        "uncached_prompt": legacyUncachedPrompt,
        "projection_mode": Self.legacyProjectionMode,
        "source_builders": [
          "ContextProactivityPromptBuilder.directorStablePrompt",
          "ContextProactivityPromptBuilder.directorVolatilePrompt",
        ],
        "flags": [
          "allow_lookup=false",
          "include_interject_copy_budgets=false",
          "workstream_pooling=false",
          "proactive_candidates=false",
        ],
      ],
      "nano": [
        "prompt": nanoPrompt,
        "source_builder": "JITProactivityPromptBuilder.nanoTriagePrompt",
      ],
      "full": [
        "prompt": fullPrompt,
        "source_builder": "JITProactivityPromptBuilder.fullTurnPrompt",
      ],
    ]
  }
}
