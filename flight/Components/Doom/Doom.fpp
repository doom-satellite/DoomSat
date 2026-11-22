module DoomMission {

    @ What the onboard navigator steers toward. Chosen on the ground, executed onboard.
    enum Goal : U8 {
        EXPLORE = 0        @< Explore toward the nearest unexplored frontier (the exit is found, not known)
        KILL_ENEMY = 1     @< Face and close on the nearest visible enemy
        STOCK_AMMO = 2     @< Route to the nearest ammunition pickup
        RESTORE_HEALTH = 3 @< Route to the nearest health pickup
        ADD_ARMOR = 4      @< Route to the nearest armor pickup
        UPGRADE_WEAPON = 5 @< Route to the nearest weapon pickup
        SCOUT = 6          @< Explore toward the farthest frontier (a different part of the level)
        HOLD = 7           @< Stand still (safe mode)
    }

    @ What the range camera and the map say is at arm's length ahead
    enum AheadKind : U8 {
        NOTHING = 0
        WALL = 1
        DOOR = 2
        EXIT = 3           @< The exit line, seen on the automap
        LOCKED = 4         @< A locked door without its key
        BARRIER = 5        @< Something the map does not show (window bars, a fake door)
        THING = 6          @< A monster or a barrel
    }

    @ Weapon the player currently holds
    enum Weapon : U8 {
        FIST = 0
        PISTOL = 1
        SHOTGUN = 2
        OTHER = 3
    }

    @ What the onboard executor is being asked to do (charter 3.1)
    enum IntentMode : U8 {
        EXPLORE = 0
        APPROACH = 1
        OPERATE = 2
        FIGHT = 3
        RETREAT = 4
        RECOVER = 5
    }

    @ How to move while carrying out an intent
    enum Stance : U8 {
        ADVANCE = 0
        ADVANCE_STRAFING = 1
        HOLD = 2
        RETREAT = 3
    }

    @ What the executor is allowed to shoot at
    enum FirePolicy : U8 {
        NONE = 0
        ANY_ATTACKER = 1
        NEAREST = 2
        TARGET = 3
    }

    @ What kind of place a candidate target is
    enum CandKind : U8 {
        FRONTIER = 0
        DOOR = 1
        EXIT = 2
        KEY = 3
        ITEM = 4
        SWITCH = 5
        ENEMY = 6
    }

    @ Somewhere worth going, as the onboard world model offers it to the ground (charter 3.3).
    @ The distance is along walkable floor, not the straight line: a frontier 200 units away through a
    @ wall is not 200 units away, and scoring it as though it were is how a pilot walks into the same
    @ corner all afternoon.
    struct Candidate {
        kind: CandKind
        x: F32          @< where it is, in map units
        y: F32
        pathUnits: U16  @< distance along the floor the payload has actually seen
        novelty: U8     @< how much unseen ground lies behind it
        flags: U8       @< bits 0-1 key colour (0 none, 1 red, 2 blue, 3 yellow), bits 2-5 tries so far
        threatClass: U8 @< worst monster class standing near it, by index; 255 = nothing there
        threatCount: U8 @< how many live things are near it
        opening: U16    @< how wide the way on is, in map units
        depth: U16      @< how far the unknown runs past it
        away: U8        @< 1 when it leads away from the ground already walked
    }

    @ !binary
    @ Raw bytes of one slice of a JPEG frame (opaque blob on the ground)
    array ChunkBytes = [960] U8

    @ One slice of a JPEG frame, downlinked as an image product. Reassemble on the ground by (seq, index).
    struct FrameChunk {
        seq: U32     @< Frame sequence number
        index: U16   @< Chunk index within the frame
        count: U16   @< Number of chunks in the frame
        length: U16  @< Valid bytes in data
        data: ChunkBytes
    }

    @ !binary
    @ A WAD file name in ASCII, padded with zeros. Bytes rather than a string: fprime-xtce gives F' string
    @ telemetry a fixed size while F' sends it length-prefixed, and Yamcs then rejects the whole packet.
    array WadName = [40] U8

    @ Payload interface component: bridges the Doom game process (payload) to F Prime commands and telemetry.
    active component Doom {

        # ----------------------------------------------------------------------
        # Ports
        # ----------------------------------------------------------------------

        @ Rate group input: polls the payload socket and downlinks what arrived
        async input port run: Svc.Sched

        @ Image product packets (FrameChunk telemetry records) sent straight to the com queue
        output port frameOut: Fw.Com

        @ An uplinked WAD known to be whole: it arrives as NAME.wad.<anything>.part in the uplink directory and
        @ becomes NAME.wad here. On the CFDP build cfdpGuard announces it when the receiver's FIN says the transfer
        @ ended with no error and the file retained; on the native build FileUplink announces each file whose
        @ checksum it has verified. A path outside the uplink directory is refused.
        sync input port fileAnnounce: Svc.FileAnnounce

        # ----------------------------------------------------------------------
        # Commands (uplink)
        # ----------------------------------------------------------------------

        @ One set of held controls; applied every game tic until the next CONTROL arrives
        async command CONTROL(
            move: I8      @< -1 backward, 0 hold, 1 forward
            strafe: I8    @< -1 left, 0 hold, 1 right
            turn: F32     @< degrees to turn (heading setpoint executed onboard), positive left
            fire: bool    @< hold the trigger
            $use: bool    @< press use (doors, switches)
            weapon: Weapon @< weapon to select (FIST/OTHER = keep current)
        ) opcode 0x00

        @ What to do and for how long, instead of buttons for one tic (charter 3.1). The onboard executor
        @ carries it out at game rate -- following the planned path, avoiding what the range camera sees,
        @ aiming, firing, pressing Use -- and drops to safe behaviour when ttl_ms lapses. The player never
        @ stands still waiting for the ground.
        async command INTENT(
            intentId: U16      @< rises with every intent; the newest wins
            basedOnTic: U32    @< the observation this was decided from; an older answer is dropped
            mode: IntentMode
            targetX: F32       @< where to go, in map units
            targetY: F32
            hasTarget: bool    @< false means "no destination, act where you stand"
            stance: Stance
            firePolicy: FirePolicy
            fireTargetId: U8   @< candidate index for FIRE_TARGET, 255 = none
            weapon: U8         @< weapon slot 1-5, 255 = keep the current one
            useAtTarget: bool  @< press Use on arrival (a door, a switch, the exit)
            ttlMs: U16         @< how long this intent is worth acting on
        ) opcode 0x05

        @ Set the navigation goal the onboard navigator routes toward
        async command SET_GOAL(goal: Goal) opcode 0x01

        @ Restart the episode
        async command RESET_GAME opcode 0x02

        @ Bias exploration toward a direction for a while (what the ground saw in a frame)
        async command EXPLORE_HINT(
            bearing: I16  @< degrees relative to the current heading, positive left
            ttl: U8       @< seconds the hint stays in force
        ) opcode 0x04

        @ Set the frame downlink rate and JPEG quality
        async command FRAME_RATE(
            hz: U8        @< frames per second (0 disables frames)
            quality: U8   @< JPEG quality 10-95
        ) opcode 0x03

        @ Switch the game to another level file without restarting anything (an uplinked WAD, or one already
        @ installed). Names are bare file names ending in .wad, found in the uplink directory or the installed
        @ WAD directory. The payload proves the game starts on them in a separate process before it rebuilds
        @ its own game and starts a fresh episode; the outcome comes back as WadLoaded or WadLoadFailed, and on
        @ failure the game carries on with the WAD it had. Safe to send again: one for the files and map already
        @ flying changes nothing and answers WadAlreadyFlying (RESET_GAME restarts the level), and one that repeats
        @ the load being proven gets that load's answer.
        async command LOAD_WAD(
            iwad: string size 40  @< the IWAD (freedoom2.wad, doom1.wad, ...)
            pwad: string size 40  @< a PWAD to load over it; empty for none
            $map: string size 10  @< the map to start on (Yamcs counts the length tag, so 10 carries 8)
        ) opcode 0x06

        @ Put an uplinked WAD in place: rename UPLINK/NAME.wad.<nonce>.part to UPLINK/NAME.wad, where UPLINK is
        @ $DOOMSAT_HOME/wads/uplink. fileAnnounce does this by itself for a Class 2 upload (cfdpGuard) or a native
        @ one; this command is for the rest (a Class 1 upload, or a commit by hand), once the transfer has finished.
        @ The file is renamed only if its size and CFDP checksum are the ones the ground sent: a commit sent before
        @ the whole file is on board, or after a damaged Class 1 upload, leaves it a .part (WadCommitRefused) and
        @ LOAD_WAD cannot use it. Safe to send again: with no .part, a commit of an upload already put in place as
        @ NAME.wad (since start, by this command or cfdpGuard; the record holds eight names), NAME.wad still with that
        @ size and checksum, answers WadUplinked as the first commit did; anything else is WadUplinkFailed. Wait for
        @ the FIN: a commit between the last byte and cfdpManager's CRC pass renames the whole file, but the transfer
        @ then ends with a file-size error.
        async command COMMIT_WAD(
            part: string size 40  @< the bare name it was uplinked under: NAME.wad.<nonce>.part
            fileSize: U32  @< the size the ground sent, in bytes
            checksum: U32  @< the CFDP modular checksum of what the ground sent (the one its EOF carries)
        ) opcode 0x07

        # ----------------------------------------------------------------------
        # Telemetry (downlink)
        # ----------------------------------------------------------------------

        telemetry HEALTH: I16 id 0
        telemetry ARMOR: I16 id 1
        telemetry SHELLS: I16 id 2
        telemetry BULLETS: I16 id 3
        telemetry WEAPON: Weapon id 4
        telemetry OWN_SHOTGUN: bool id 5
        telemetry KILLS: U16 id 6
        telemetry POS_X: F32 id 7
        telemetry POS_Y: F32 id 8
        telemetry ANGLE: F32 id 9
        telemetry ENEMY_COUNT: U8 id 10
        telemetry ENEMY_BEARING: F32 id 11 @< degrees, positive left
        telemetry ENEMY_DIST: U16 id 12
        telemetry CLEAR_FWD: U16 id 13 @< map units of free space straight ahead (range camera)
        telemetry CLEAR_LEFT: U16 id 14 @< map units of open way to the left (map ray)
        telemetry CLEAR_RIGHT: U16 id 15
        telemetry CLEAR_BACK: U16 id 16
        telemetry CLEAR_FL: U16 id 17 @< range camera, ahead-left band
        telemetry CLEAR_FR: U16 id 18 @< range camera, ahead-right band
        telemetry CLEAR_MAP_FWD: U16 id 19 @< map ray straight ahead
        telemetry NEW_FWD: U8 id 20 @< percent of the ground that way not yet walked
        telemetry NEW_LEFT: U8 id 21
        telemetry NEW_RIGHT: U8 id 22
        telemetry NEW_BACK: U8 id 23
        telemetry AHEAD_KIND: AheadKind id 24 @< what is at arm's length ahead
        telemetry AHEAD_DIST: U16 id 25
        telemetry EXIT_BEARING: F32 id 26 @< degrees to the exit line seen, positive left
        telemetry EXIT_DIST: U16 id 27 @< 0 when no exit line has been seen
        telemetry KEY_BEARING: F32 id 28
        telemetry KEY_DIST: U16 id 29 @< 0 when no key is remembered
        telemetry HEALTH_ITEM_DIST: U16 id 30
        telemetry AMMO_ITEM_DIST: U16 id 31
        telemetry ARMOR_ITEM_DIST: U16 id 32
        telemetry HEALTH_BEARING: F32 id 33
        telemetry AMMO_BEARING: F32 id 34
        telemetry ARMOR_BEARING: F32 id 35
        telemetry STUCK: bool id 36
        telemetry DOOR_AHEAD: bool id 37 @< something usable at arm's length
        telemetry GOAL: Goal id 38
        telemetry TIC: U32 id 39
        telemetry EPISODE: U16 id 40
        telemetry DEAD: bool id 41
        telemetry LEVEL_DONE: bool id 42
        telemetry EXPLORED_CELLS: U16 id 43 @< cells of the self-built map the player has stood in
        telemetry LEVEL: U8 id 44 @< levels started so far (1 = the first map)
        telemetry KEYS: U8 id 45 @< keys held, bitmask red=1 blue=2 yellow=4
        telemetry HINT_ACTIVE: bool id 46 @< an exploration hint from the ground is in force
        telemetry HINT_REL: I16 id 47 @< the hint's bearing relative to the heading, degrees
        telemetry FRAMES_SENT: U32 id 48
        telemetry CHUNKS_SENT: U32 id 49
        telemetry FRAME_BYTES: U32 id 50 @< bytes of the last frame
        telemetry PAYLOAD_LINK: bool id 51
        telemetry CMDS_RECEIVED: U32 id 52
        telemetry FRAME_CHUNK: FrameChunk id 53
        telemetry CLEAR_AL: U16 id 54 @< map ray ahead-left (45 deg)
        telemetry CLEAR_AR: U16 id 55 @< map ray ahead-right
        telemetry CLEAR_BL: U16 id 56 @< map ray behind-left (135 deg)
        telemetry CLEAR_BR: U16 id 57 @< map ray behind-right
        telemetry NEW_AL: U8 id 58 @< percent of the ground ahead-left not yet walked
        telemetry NEW_AR: U8 id 59
        telemetry NEW_BL: U8 id 60
        telemetry NEW_BR: U8 id 61
        # Door distance per sector in 8-unit steps, 0 = no door on the way. Separate from NEW_*, which is
        # novelty only: overloading one channel with both lost the novelty wherever a door was seen.
        telemetry DOOR_FWD: U8 id 62 @< door on the forward ray, distance / 8 units (0 = none)
        telemetry DOOR_AL: U8 id 63
        telemetry DOOR_LEFT: U8 id 64
        telemetry DOOR_BL: U8 id 65
        telemetry DOOR_BACK: U8 id 66
        telemetry DOOR_BR: U8 id 67
        telemetry DOOR_RIGHT: U8 id 68
        telemetry DOOR_AR: U8 id 69

        @ The candidate targets the onboard world model offers for scoring (charter 3.3)
        telemetry CAND_COUNT: U8 id 70 @< how many of the eight slots below are filled
        telemetry CAND0: Candidate id 71
        telemetry CAND1: Candidate id 72
        telemetry CAND2: Candidate id 73
        telemetry CAND3: Candidate id 74
        telemetry CAND4: Candidate id 75
        telemetry CAND5: Candidate id 76
        telemetry CAND6: Candidate id 77
        telemetry CAND7: Candidate id 78
        telemetry INTENT_ID: U16 id 79 @< the intent the executor is currently carrying out
        telemetry WATCHDOG_TRIPS: U16 id 80 @< times an invariant had to pull the player out of a freeze

        @ What is threatening the player right now (charter 4, the engage head). The class is a fact;
        @ how dangerous it is comes from the knowledge file on the ground.
        telemetry THREAT_CLASS: U8 id 81 @< worst visible monster class by index; 255 = nothing in view
        telemetry THREAT_COUNT: U8 id 82 @< how many are in view

        @ Use presses, and the ones that opened something. The ratio is door_precision: whether
        @ the automap's ceiling-change category is telling the truth about what is a door.
        telemetry DOOR_PRESSES: U16 id 83
        telemetry DOOR_OPENS: U16 id 84

        # The level file the game is running on (LOAD_WAD)
        telemetry WAD_IWAD: WadName id 85 @< the IWAD
        telemetry WAD_PWAD: WadName id 86 @< the PWAD loaded over it; zeros for none
        telemetry WAD_LOADS: U16 id 87 @< LOAD_WAD commands that switched the game

        # ----------------------------------------------------------------------
        # Events
        # ----------------------------------------------------------------------

        event PayloadConnected severity activity high id 0 format "Doom payload connected"
        event PayloadLost severity warning high id 1 format "Doom payload link lost"
        event EpisodeStarted(episode: U16) severity activity high id 2 format "Episode {} started"
        event PlayerDied(episode: U16, tic: U32) severity warning low id 3 format "Player died in episode {} at tic {}"
        event LevelFinished(episode: U16, tic: U32) severity activity high id 4 format "Level finished in episode {} at tic {}"
        event GoalSet(goal: Goal) severity activity low id 5 format "Navigation goal set to {}"
        event ExploreHint(bearing: I16, ttl: U8) severity activity low id 8 format "Exploration hint {} degrees for {} s"
        event FrameTooLarge(bytes: U32) severity warning low id 6 format "Frame of {} bytes exceeds the chunk budget; dropped"
        event BadPayloadMessage(kind: U8) severity warning low id 7 format "Unknown payload message kind {}"
        event LevelStarted(level: U8) severity activity high id 9 format "Now playing level {}"
        event KeyPickedUp(keys: U8) severity activity high id 10 format "Keys held (bitmask red=1 blue=2 yellow=4): {}"
        event IntentSet(intentId: U16, mode: IntentMode, ttlMs: U16) severity activity low id 11 format "Intent {} {} for {} ms"
        event WadLoaded(name: string size 90, $map: string size 8) severity activity high id 12 format "Now flying {} on {}"
        event WadLoadFailed(name: string size 90, reason: string size 120) severity warning high id 13 format "Could not load {}: {}"
        event WadUplinked(fileName: string size 120) severity activity high id 14 format "Uplinked WAD ready to load: {}"
        event WadUplinkFailed(fileName: string size 120) severity warning high id 15 format "Uplinked WAD could not be renamed into place: {}"
        event WadCommitRefused(fileName: string size 120, haveSize: U64, haveChecksum: U32, wantSize: U32, wantChecksum: U32) severity warning high id 16 format "Uplinked WAD not committed, it is not the file the ground sent: {} has {} bytes, checksum 0x{x}; expected {} bytes, checksum 0x{x}"
        event WadAlreadyFlying(name: string size 90, $map: string size 8) severity activity high id 17 format "Already flying {} on {}: LOAD_WAD changed nothing"

        # ----------------------------------------------------------------------
        # Standard ports
        # ----------------------------------------------------------------------

        command recv port cmdIn
        command reg port cmdRegOut
        command resp port cmdResponseOut
        event port eventOut
        text event port textEventOut
        time get port timeCaller
        telemetry port tlmOut
    }
}
