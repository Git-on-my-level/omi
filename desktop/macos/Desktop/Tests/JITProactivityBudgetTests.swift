import Darwin
import Foundation
import XCTest

@testable import Omi_Computer

final class JITProactivityBudgetTests: XCTestCase {
  func testTemporalPromptLabelsCaptureAndEvaluationSeparately() {
    let capturedAt = Date(timeIntervalSince1970: 1_775_000_000)
    let evaluatedAt = capturedAt.addingTimeInterval(12)
    let temporal = JITProactivityTemporalContext(
      capturedAt: capturedAt,
      evaluatedAt: evaluatedAt,
      timezoneIdentifier: "America/New_York")

    let prompt = temporal.promptSection()

    XCTAssertTrue(prompt.contains("Evidence captured at (UTC):"))
    XCTAssertTrue(prompt.contains("Evaluated at (UTC):"))
    XCTAssertTrue(prompt.contains("Authoritative user timezone: America/New_York"))
    XCTAssertTrue(prompt.contains("Do not make a time-specific claim"))
    XCTAssertNotEqual(capturedAt, evaluatedAt)
  }

  func testMissingTemporalContextDoesNotInventAClock() {
    let temporal = JITProactivityTemporalContext(
      capturedAt: nil, evaluatedAt: nil, timezoneIdentifier: nil)

    let prompt = temporal.promptSection()

    XCTAssertTrue(prompt.contains("Evidence capture time: unavailable"))
    XCTAssertTrue(prompt.contains("Evaluation time: unavailable"))
    XCTAssertTrue(prompt.contains("Authoritative user timezone: unavailable"))
    XCTAssertTrue(prompt.contains("Do not make a time-specific claim"))
  }

  func testSharedFullTurnPromptRetainsAuthoritativeTemporalContext() {
    let temporal = JITProactivityTemporalContext(
      capturedAt: Date(timeIntervalSince1970: 1_775_000_000),
      evaluatedAt: Date(timeIntervalSince1970: 1_775_000_012),
      timezoneIdentifier: "America/New_York")
    let prompt = JITProactivityPromptBuilder.fullTurnPrompt(
      lane: .planned,
      executionPrompt: "Review the deadline",
      currentEvidence: "fact:deadline-1 The deadline is tomorrow.",
      derivedIntent: JITDerivedIntentMatch(entries: []),
      ambientEvidence: "",
      temporalContext: temporal)
    XCTAssertTrue(prompt.contains(temporal.promptSection()))
    XCTAssertTrue(prompt.contains("fact:deadline-1"))
    XCTAssertTrue(prompt.contains("write tools and external actions"))

    let unavailable = JITProactivityPromptBuilder.fullTurnPrompt(
      lane: .ambient,
      executionPrompt: "Review the screen",
      currentEvidence: "fact:screen-1 A draft is open.",
      derivedIntent: JITDerivedIntentMatch(entries: []),
      ambientEvidence: "")
    XCTAssertTrue(unavailable.contains("Trusted temporal context: unavailable"))
    XCTAssertFalse(unavailable.contains("America/New_York"))
  }

  func testBudgetOnlyAttachesForAdvertisedQualificationContract() {
    let executionID = String(repeating: "a", count: 64)
    let budget = JITProactivityAgentBudget(
      contractVersion: JITProactivityAgentBudget.cloudQAContractVersion,
      executionID: executionID)

    XCTAssertNotNil(budget)
    XCTAssertEqual(budget?.maxProviderAttempts, 3)
    XCTAssertEqual(budget?.maxOutputTokensPerAttempt, 2_048)
    XCTAssertEqual(budget?.maxNormalizedInputTokensPerAttempt, 32_768)
    XCTAssertEqual(budget?.maxEstimatedSpendMicroUSD, 50_000)
    XCTAssertNil(JITProactivityAgentBudget(contractVersion: nil, executionID: executionID))
    XCTAssertNil(JITProactivityAgentBudget(contractVersion: "old", executionID: executionID))
  }

  func testSourceProjectionIsFixedQAOnlyAndUsesTheAdmittedExecutionTuple() throws {
    let executionID = String(repeating: "a", count: 64)
    let temporal = JITProactivityTemporalContext(
      capturedAt: Date(timeIntervalSince1970: 1_775_000_000),
      evaluatedAt: Date(timeIntervalSince1970: 1_775_000_012),
      timezoneIdentifier: "America/New_York")
    let execution = JITPlannedExecution(
      lane: .ambient,
      triggerID: "ambient:context",
      continuityKey: "continuity",
      prompt: "full",
      claim: JITTriggerWakeupClaim(
        continuityKey: "continuity", triggerID: "ambient:context", leaseToken: "lease"),
      plannedAuthority: nil,
      candidateID: executionID,
      accountGeneration: 1,
      policy: .ratifiedV1,
      temporalContext: temporal,
      agentBudget: JITProactivityAgentBudget(
        contractVersion: JITProactivityAgentBudget.cloudQAContractVersion,
        executionID: executionID),
      nanoPrompt: "nano")

    let makeProjection: (String, String) -> JITProactivitySourceProjection? = { bundle, owner in
      JITProactivitySourceProjection.makeIfPermitted(
        execution: execution,
        ownerID: owner,
        contextID: "bucket-1",
        legacyPrompt: "legacy",
        legacyUncachedPrompt: "legacy-uncached",
        nanoPrompt: "nano",
        fullPrompt: "full",
        bundleIdentifier: bundle)
    }
    XCTAssertNil(makeProjection("com.omi.omi", JITProactivitySourceProjection.qaOwnerID))
    XCTAssertNil(
      makeProjection(
        JITProactivitySourceProjection.qaBundleIdentifier, "different-owner"))

    let projection = try XCTUnwrap(
      makeProjection(
        JITProactivitySourceProjection.qaBundleIdentifier,
        JITProactivitySourceProjection.qaOwnerID))
    XCTAssertEqual(projection.executionID, executionID)
    XCTAssertEqual(projection.producerLane, .ambient)
    XCTAssertEqual(projection.timezone, "America/New_York")
    XCTAssertTrue(projection.evaluationTime.contains("-04:00"))
    let wire = projection.wireDictionary
    XCTAssertEqual(wire["schema_version"] as? String, JITProactivitySourceProjection.schemaVersion)
    XCTAssertEqual(wire["execution_id"] as? String, executionID)
    XCTAssertEqual(wire["producer_lane"] as? String, "ambient")
    let legacy = try XCTUnwrap(wire["legacy"] as? [String: Any])
    XCTAssertEqual(legacy["projection_mode"] as? String, "director_baseline_v1")
    XCTAssertEqual(
      legacy["source_builders"] as? [String],
      [
        "ContextProactivityPromptBuilder.directorStablePrompt",
        "ContextProactivityPromptBuilder.directorVolatilePrompt",
      ])
  }

  func testQAStoragePreflightRequiresOwnerOnlyDirectoryAndSQLite() throws {
    let directory = FileManager.default.homeDirectoryForCurrentUser
      .appendingPathComponent("jit-qa-state-\(UUID().uuidString)")
    defer { try? FileManager.default.removeItem(at: directory) }
    try FileManager.default.createDirectory(
      at: directory, withIntermediateDirectories: true,
      attributes: [.posixPermissions: 0o700])
    let database = directory.appendingPathComponent("omi-agentd.sqlite3")
    XCTAssertTrue(
      FileManager.default.createFile(
        atPath: database.path, contents: Data(), attributes: [.posixPermissions: 0o600]))
    XCTAssertTrue(
      AgentRuntimeProcess.hasPrivateJITQAStateDirectory(
        bundleIdentifier: JITProactivitySourceProjection.qaBundleIdentifier,
        stateDirectory: directory))

    let foreignOwner: uid_t = getuid() == 0 ? 1 : 0
    XCTAssertFalse(
      AgentRuntimeProcess.hasPrivateJITQAStateDirectory(
        bundleIdentifier: JITProactivitySourceProjection.qaBundleIdentifier,
        stateDirectory: directory,
        attributesProvider: { path in
          guard var attributes = try? FileManager.default.attributesOfItem(atPath: path) else {
            return nil
          }
          if path == directory.path || path == database.path {
            attributes[.ownerAccountID] = NSNumber(value: foreignOwner)
          }
          return attributes
        }))

    try FileManager.default.setAttributes(
      [.posixPermissions: 0o755], ofItemAtPath: directory.path)
    XCTAssertFalse(
      AgentRuntimeProcess.hasPrivateJITQAStateDirectory(
        bundleIdentifier: JITProactivitySourceProjection.qaBundleIdentifier,
        stateDirectory: directory))

    try FileManager.default.setAttributes(
      [.posixPermissions: 0o700], ofItemAtPath: directory.path)
    try FileManager.default.removeItem(at: database)
    XCTAssertTrue(
      FileManager.default.createFile(
        atPath: database.path, contents: Data(), attributes: [.posixPermissions: 0o600]))
    let danglingWAL = URL(fileURLWithPath: database.path + "-wal")
    let danglingTarget = directory.appendingPathComponent("missing-wal-target")
    try FileManager.default.createSymbolicLink(
      at: danglingWAL, withDestinationURL: danglingTarget)
    XCTAssertFalse(
      AgentRuntimeProcess.hasPrivateJITQAStateDirectory(
        bundleIdentifier: JITProactivitySourceProjection.qaBundleIdentifier,
        stateDirectory: directory))
    try? FileManager.default.removeItem(at: danglingWAL)

    try FileManager.default.removeItem(at: database)
    let outsideDatabase = FileManager.default.homeDirectoryForCurrentUser
      .appendingPathComponent("jit-qa-database-\(UUID().uuidString)")
    defer { try? FileManager.default.removeItem(at: outsideDatabase) }
    XCTAssertTrue(
      FileManager.default.createFile(
        atPath: outsideDatabase.path, contents: Data(), attributes: [.posixPermissions: 0o600]))
    try FileManager.default.createSymbolicLink(
      at: database, withDestinationURL: outsideDatabase)
    XCTAssertFalse(
      AgentRuntimeProcess.hasPrivateJITQAStateDirectory(
        bundleIdentifier: JITProactivitySourceProjection.qaBundleIdentifier,
        stateDirectory: directory))
  }
}
