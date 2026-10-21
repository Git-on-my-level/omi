import 'dart:async';
import 'dart:io';

import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/services/devices/connectors/device_connection.dart';
import 'package:omi/services/wals/sdcard_wal_sync.dart';
import 'package:omi/services/wals/wal.dart';
import 'package:omi/services/wals/wal_interfaces.dart';

class _Listener implements IWalSyncListener {
  @override
  void onWalUpdated() {}

  @override
  void onWalSynced(Wal wal, {ServerConversation? conversation}) {}
}

class _FakeLocalSync implements LocalWalSync {
  _FakeLocalSync();

  bool admit = true;
  int? failOnChunk;
  final added = <Wal>[];
  int admissionCalls = 0;
  int admissionCallOrder = -1;
  int releasedBytes = 0;

  static int eventSeq = 0;
  static int nextEvent() => ++eventSeq;

  @override
  int get sessionGeneration => 1;

  @override
  Future<void> addExternalWal(Wal wal, {required int admittedGeneration}) async {
    if (failOnChunk == added.length + 1) throw const FileSystemException('index write failed');
    added.add(wal);
  }

  @override
  Future<bool> ensureStorageAdmission({required int bytes, required int admittedGeneration}) async {
    admissionCalls++;
    if (admissionCallOrder < 0) admissionCallOrder = nextEvent();
    return admit;
  }

  @override
  Future<bool> hasDurableWal(Wal wal, {required int admittedGeneration}) async => added.contains(wal);

  @override
  void releaseStorageAdmission(int bytes) {
    releasedBytes += bytes;
  }

  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}

class _FakeSdCard implements DeviceConnection {
  _FakeSdCard({required this.packets, this.clearAccepted = true});

  final List<List<int>> packets;
  final bool clearAccepted;
  final writes = <List<int>>[];
  void Function(List<int>)? _onBytes;
  int firstWriteEvent = -1;

  bool get cleared => writes.any((write) => write[1] == 1 && _clearOk);

  bool _clearOk = false;

  @override
  Future<StreamSubscription?> getBleStorageBytesListener({
    required void Function(List<int>) onStorageBytesReceived,
  }) async {
    _onBytes = onStorageBytesReceived;
    return null;
  }

  @override
  Future<bool> writeToStorage(int numFile, int command, int offset) async {
    if (firstWriteEvent < 0) firstWriteEvent = _FakeLocalSync.nextEvent();
    writes.add([numFile, command, offset]);
    if (command == 0) {
      scheduleMicrotask(() {
        for (final p in packets) {
          _onBytes!(p);
        }
      });
    }
    if (command == 1) {
      _clearOk = clearAccepted;
      return clearAccepted;
    }
    return true;
  }

  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}

List<int> _legacyPacket(int frameLen) {
  return List<int>.filled(83, 0)..[3] = frameLen;
}

List<int> _packed440(List<int> frameSizes, {int? trailingSizeByte, int? markerEpoch, int markerAt = -1}) {
  final buf = <int>[];
  int i = 0;
  for (final size in frameSizes) {
    if (i == markerAt && markerEpoch != null) {
      buf.add(0xFF);
      buf.add(markerEpoch & 0xFF);
      buf.add((markerEpoch >> 8) & 0xFF);
      buf.add((markerEpoch >> 16) & 0xFF);
      buf.add((markerEpoch >> 24) & 0xFF);
    }
    buf.add(size);
    buf.addAll(List<int>.filled(size, 0xAB));
    i++;
  }
  if (trailingSizeByte != null) buf.add(trailingSizeByte);
  return List<int>.from(buf)..addAll(List<int>.filled(440 - buf.length, 0));
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  late Directory tempDir;

  setUp(() async {
    _FakeLocalSync.eventSeq = 0;
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
    tempDir = await Directory.systemTemp.createTemp('sdcard_custody_test_');
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(
      const MethodChannel('plugins.flutter.io/path_provider'),
      (MethodCall methodCall) async {
        if (methodCall.method == 'getApplicationDocumentsDirectory') return tempDir.path;
        return null;
      },
    );
  });

  tearDown(() async {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(
      const MethodChannel('plugins.flutter.io/path_provider'),
      null,
    );
    if (tempDir.existsSync()) tempDir.deleteSync(recursive: true);
  });

  Wal makeWal({required int totalBytes, int seconds = 5}) => Wal(
        timerStart: 1000,
        codec: BleAudioCodec.opus,
        seconds: seconds,
        status: WalStatus.miss,
        storage: WalStorage.sdcard,
        device: 'devkit-1',
        fileNum: 1,
        storageOffset: 0,
        storageTotalBytes: totalBytes,
      );

  SDCardWalSyncImpl syncWith(_FakeLocalSync local, _FakeSdCard card, Wal wal, {String firmware = '3.0.10'}) {
    final sync = SDCardWalSyncImpl(_Listener())
      ..testDevice = BtDevice(
        id: 'devkit-1',
        name: 'Omi DevKit',
        type: DeviceType.omi,
        rssi: -40,
        firmwareRevision: firmware,
      )
      ..testConnection = card
      ..testWals = [wal];
    sync.setLocalSync(local);
    return sync;
  }

  test('admission is consulted before any chunk file is written; refusal writes nothing and keeps the card', () async {
    const packets = 5; // 5 * 80 bytes of legacy packets
    final wal = makeWal(totalBytes: packets * 80, seconds: 1);
    final local = _FakeLocalSync()..admit = false;
    final card = _FakeSdCard(
      packets: [
        for (var i = 0; i < packets; i++) _legacyPacket(10),
        [100],
      ],
    );
    final sync = syncWith(local, card, wal);

    await expectLater(sync.syncWal(wal: wal), throwsA(anything));

    expect(local.admissionCalls, greaterThan(0));
    expect(local.added, isEmpty);
    expect(tempDir.listSync().whereType<File>(), isEmpty);
    expect(card.cleared, isFalse);
    expect(wal.status, isNot(WalStatus.synced));
  });

  test('a rejected CLEAR keeps the wal as miss and reports failure', () async {
    const packets = 3;
    final wal = makeWal(totalBytes: packets * 80, seconds: 1);
    final local = _FakeLocalSync();
    final card = _FakeSdCard(
      packets: [
        for (var i = 0; i < packets; i++) _legacyPacket(10),
        [100],
      ],
      clearAccepted: false,
    );
    final sync = syncWith(local, card, wal);

    await expectLater(sync.syncWal(wal: wal), throwsA(anything));

    expect(local.added, isNotEmpty);
    expect(wal.status, isNot(WalStatus.synced));
  });

  test('packed packets ending in an orphan trailing size byte still complete cleanly', () async {
    const frameLen = 40; // [1 + 40] * 10 = 410 bytes, +1 trailing size byte
    final packet = _packed440(List.filled(10, frameLen), trailingSizeByte: 80);
    final wal = makeWal(totalBytes: 440, seconds: 1);
    final local = _FakeLocalSync();
    final card = _FakeSdCard(
      packets: [
        packet,
        [100],
      ],
    );
    final sync = syncWith(local, card, wal);

    await sync.syncWal(wal: wal);

    expect(local.added, hasLength(1));
    expect(local.added.first.totalFrames, 10);
    expect(card.cleared, isTrue);
    expect(wal.status, WalStatus.synced);
  });

  test('a failed chunk keeps the restart cursor at the original offset and the card intact', () async {
    const frameLen = 40;
    const epochMarker = 1700000000;
    final wal = makeWal(totalBytes: 3 * 440, seconds: 1);
    final local = _FakeLocalSync()..failOnChunk = 2;
    final card = _FakeSdCard(
      packets: [
        _packed440(List.filled(2, frameLen)),
        _packed440(List.filled(2, frameLen)),
        _packed440(List.filled(3, frameLen), markerEpoch: epochMarker, markerAt: 0),
        [100],
      ],
    );
    final sync = syncWith(local, card, wal, firmware: '3.0.17');

    await expectLater(sync.syncWal(wal: wal), throwsA(anything));

    expect(local.added, hasLength(1), reason: 'the first chunk was durable before the failure');
    expect(wal.storageOffset, 0, reason: 'partial progress must never become the restart cursor');
    expect(card.cleared, isFalse);
    expect(wal.status, isNot(WalStatus.synced));
  });

  test('a short transfer that ends before the expected offset is not cleared', () async {
    final wal = makeWal(totalBytes: 3 * 80, seconds: 1);
    final local = _FakeLocalSync();
    final card = _FakeSdCard(
      packets: [
        _legacyPacket(10),
        [100],
      ],
    );
    final sync = syncWith(local, card, wal);

    await expectLater(sync.syncWal(wal: wal), throwsA(anything));

    expect(card.cleared, isFalse);
    expect(wal.status, isNot(WalStatus.synced));
  });

  test('marker path: timestamp marker splits chunks and applies its epoch to the next segment', () async {
    const frameLen = 40;
    const epochMarker = 1700000000;
    final packets = <List<int>>[
      _packed440(List.filled(2, frameLen)),
      _packed440(List.filled(2, frameLen)),
      _packed440(List.filled(3, frameLen), markerEpoch: epochMarker, markerAt: 0),
      [100],
    ];
    final wal = makeWal(totalBytes: 3 * 440, seconds: 1);
    wal.timerStart = 1111;
    final local = _FakeLocalSync();
    final card = _FakeSdCard(packets: packets);
    final sync = syncWith(local, card, wal, firmware: '3.0.17');

    await sync.syncWal(wal: wal);

    expect(local.added, hasLength(2));
    expect(local.added[0].totalFrames, 4);
    expect(local.added[0].timerStart, 1111);
    expect(local.added[1].totalFrames, 3);
    expect(local.added[1].timerStart, epochMarker);
    expect(card.cleared, isTrue);
    expect(wal.status, WalStatus.synced);
  });

  test('marker path: a marker at an exact chunk boundary is still applied', () async {
    const frameLen = 40;
    const epochMarker = 1700000999;
    const framesPerPacket = 10;
    // sdcardChunkSizeSecs (60) * 100 fps = 6000 frames = exactly one full chunk.
    final chunkPackets = sdcardChunkSizeSecs * 100 ~/ framesPerPacket;
    final packets = <List<int>>[
      for (var i = 0; i < chunkPackets; i++) _packed440(List.filled(framesPerPacket, frameLen)),
      // The marker lands at frame index 6000 — exactly the flush frontier
      // after the first full chunk (zero residual frames before it).
      _packed440(List.filled(2, frameLen), markerEpoch: epochMarker, markerAt: 0),
      [100],
    ];
    final wal = makeWal(totalBytes: (chunkPackets + 1) * 440, seconds: 60);
    wal.timerStart = 1111;
    final local = _FakeLocalSync();
    final card = _FakeSdCard(packets: packets);
    final sync = syncWith(local, card, wal, firmware: '3.0.17');

    await sync.syncWal(wal: wal);

    expect(local.added, hasLength(2));
    expect(local.added[0].totalFrames, chunkPackets * framesPerPacket);
    expect(local.added[0].timerStart, 1111);
    // The exact-boundary marker must retime the following segment to its epoch;
    // skipping it would leave later chunks on the estimated timeline forever.
    expect(local.added[1].totalFrames, 2);
    expect(local.added[1].timerStart, epochMarker);
    expect(card.cleared, isTrue);
    expect(wal.status, WalStatus.synced);
  });

  test('an interrupted SD-card transfer retries with the same clock run until the card clears', () async {
    // Legacy firmware (no markers, no listing timestamp): every chunk is
    // estimated. A BLE drop leaves the card uncleared, so the relisted
    // recording must keep the run of its already-saved chunks — a fresh nonce
    // would split one recording into two backend partitions.
    const packets = sdcardChunkSizeSecs * 100 * 2 + 1; // 2 full chunks + tail
    List<List<int>> cardPackets() => [
          for (var i = 0; i < packets; i++) _legacyPacket(10),
          [100],
        ];

    // Connection 1: one chunk lands durable, then the transfer fails.
    final wal1 = makeWal(totalBytes: packets * 80, seconds: 60);
    final local1 = _FakeLocalSync()..failOnChunk = 2;
    final sync1 = syncWith(local1, _FakeSdCard(packets: cardPackets()), wal1);
    await expectLater(sync1.syncWal(wal: wal1), throwsA(anything));
    expect(local1.added, hasLength(1));
    final firstRun = local1.added.single.clockRun;
    expect(firstRun, isNotNull);

    // Connection 2 (fresh instance, relisted recording): same run.
    final wal2 = makeWal(totalBytes: packets * 80, seconds: 60);
    final local2 = _FakeLocalSync();
    final sync2 = syncWith(local2, _FakeSdCard(packets: cardPackets()), wal2);
    await sync2.syncWal(wal: wal2);
    expect(local2.added, isNotEmpty);
    expect(local2.added.every((w) => w.clockRun == firstRun), isTrue,
        reason: 'a retried recording must reuse the run of its saved chunks');

    // After the confirmed clear, the next recording is a new run.
    final wal3 = makeWal(totalBytes: packets * 80, seconds: 60);
    final local3 = _FakeLocalSync();
    final sync3 = syncWith(local3, _FakeSdCard(packets: cardPackets()), wal3);
    await sync3.syncWal(wal: wal3);
    expect(local3.added, isNotEmpty);
    expect(local3.added.first.clockRun, isNotNull);
    expect(local3.added.first.clockRun, isNot(firstRun));
  });

  test('a recording opened before a reconnect still transfers the relisted recording', () async {
    const packets = 3;
    final opened = makeWal(totalBytes: packets * 80, seconds: 1);
    final local = _FakeLocalSync();
    final card = _FakeSdCard(
      packets: [
        for (var i = 0; i < packets; i++) _legacyPacket(10),
        [100],
      ],
    );
    final sync = syncWith(local, card, opened);
    // The reconnect's setDevice() lists the same recording again as a new Wal.
    final relisted = makeWal(totalBytes: packets * 80, seconds: 1);
    sync.testWals = [relisted];

    await sync.syncWal(wal: opened);

    expect(local.added, hasLength(1));
    expect(card.cleared, isTrue);
    expect(relisted.status, WalStatus.synced);
    expect(relisted.isSyncing, isFalse);
  });

  test('a recording the sync does not list fails the transfer and leaves the card untouched', () async {
    final listed = makeWal(totalBytes: 3 * 80, seconds: 1);
    final local = _FakeLocalSync();
    final card = _FakeSdCard(
      packets: [
        _legacyPacket(10),
        [100],
      ],
    );
    final sync = syncWith(local, card, listed);
    final unlisted = makeWal(totalBytes: 3 * 80, seconds: 1)..timerStart = 2000;
    // The phone copy of the listed recording shares its id.
    final phoneCopy = makeWal(totalBytes: 3 * 80, seconds: 1)..storage = WalStorage.disk;

    await expectLater(sync.syncWal(wal: unlisted), throwsA(isA<Exception>()));
    await expectLater(sync.syncWal(wal: phoneCopy), throwsA(isA<Exception>()));

    expect(card.writes, isEmpty);
    expect(local.added, isEmpty);
    expect(listed.status, WalStatus.miss);
  });
}
