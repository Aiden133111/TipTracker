# TipTracker.py
By: Aiden McFadden
NOT DEVELOPED IN AFFILIATION WITH OPENTRONS
USE AT YOUR OWN RISK

## The Problem
Tracking the tipboxes on the expansion slots and in stackers are not accessibily to pipettes without explicily calling the gripper to move the empty labware away and move the new tiprack from the expansion / stacker over to replace it. Because these are explicit API calls and run time parameters affect tip usage, it makes it hard to track when tip racks need to be replaced.

## The Solution
TipTracker is a method desgined to solve this problem. It replaced pipette.pick_up_tip() commands (as well as many others) to handle when pipettes are out of tips are automatically grab tips from whatever source is available wether it be stacker or expansion slot without needed explicit API calls to any of the labware or stackers. When all tipracks are gone and more are needed, the protocol is paused and prompts the user to add a specified number of tipracks. The method will also dispose of empty tipracks in one of three ways: 1. Through the waste chute. 2. Carousel tips around but keep them on deck until no tips remain and remove manually 3. Manually without carousel. 

## Using TipTracker
The following sections will descibe how TipTracker is intended to be used, but first I will explain the different cases I have considered in my development
| Component      | Low Throughput | Medium Throughput | High Throughput |
|----------------|----------------|-------------------|-----------------|
| Gripper        |        X       |        Y          |         Y       |
| Waste Chute    |        X       |        X          |         Y       |
| Trash Bin      |        Y       |        Y          |         X       |
| Stacker        |        X       |        X          |         Y       |
| Expansion Slot |        X       |        Y          |         Y       |

Low throughput configurations dont see the benefit from this since they can fit all their tips on the deck and have no method of storing tips off deck. This method is only helpful for them to automattically pause and prompt the user to refill tips if needed. 
Medium throughput configurations have a gripper and can move tips from expansion slots (or possibly a single stacker) to the active deck, but may not have a waste chute, this method serves as a way of shuttling their tips and carouseling empty boxes around until all tips are used since there is no waste chute here
High throughput individuals have a way of disposing of empty tipracks using the waste chute and can have lots of extra tips from multiple stackers (or combinaton of stacker / expasion slots) and can use this method to acheive maximum walk away time. 

### Setting up the tracker
You begin by copy/pasting the method into your python file, shouldn't be within any other function. Create your metadata,requirements,parameter, and run funcions as normal. Add labware and pipettes to the protocol as you would normal, but do not load any tipracks. 
1. Create the TrackerObj with your configuration
```
import TipTracker
from opentrons import protocol_api
.
.
.
def run(ctx : protocol_api.ProtocolContext)
	single_50 = ctx.load_instrument('flex_1channel_50', 'left',)
	multi_50 = ctx.load_instrument('flex_8channel_1000', 'right')
	chute = ctx.load_waste_chute()
	use_gripper = True

	TrackerObject = TipTracker(
		protocol_context=ctx,
		pipette1=single_50, 
		pipette2=multi_50,
		waste_bin=chute,
		use_gripper=use_gripper = True,
		debugging=True)

```
2. Determine how your extra tipracks should be added to the deck
Expansion Slots
```
	expansion_slots_for_tips = ['A4','B4','C4']
	TrackerObject.add_expansion_slots(expansion_slots_for_tips)
```
Stackers
```
	stacker = ctx.load_stacker() #WIP
	TrackObject.load_tips_in_stacker(
		stacker=stacker,
		rack_name='opentrons_flex_filtertips_1000ul',
		quantity=6,
		lid=True)
```
3. Load Tips on deck. Add up to 3 types of tipracks at a time with a corresponding list of what slots they should be in
```
	TrackObject.add_starting_tipracks(
		tiprack1 = 'opentrons_flex_96_filtertiprack_200ul',
		slots1 = ['A1','B1','B4'],
		tiprack2 = 'opentrons_flex_96_filtertiprack_50ul',
		slots2 = ['A2','A3','B2'],
		tiprack3 = 'opentrons_flex_96_filtertiprack_1000ul',
		slots3 = ['B3'])
```
4. Assign a tiprack type to a pipette
```
	TrackObject.assign_tipracks(
		pipette = single_50,
		name = 'opentrons_flex_96_filtertiprack_50ul')
```
5. Pickup and Drop Tips
```
	for i in range (47):
		TrackObject.pick_up(
			pipette = single_50)
		TrackObject.drop_tip(
			pipette = single_50,
			return_tip = True)
```
### Setting Max rack limits 
By default the tracker will refill all tip slots for a given racktype when it runs out, but this becomes problematic if we only need one or two more tipracks close to the end of the run. As developers we must understand how many tips a protocol is going to use since this protocol uses the load-as-you-go method. We determine the amount of tips we use during a particular protocol using the 

```
TrackObject.tip_count = {rackName : int for rackname in self.tipracks} # Amount of tips pickedup
TrackObject.tip_rack_count = {rackName : int for rackname in self.tipracks} #Amount of tipracks loaded 
```
by printing the tip_count property after a protocol using `ProtocolContext.comment(f'{TrackObject.tip_count}')` and celing divding all counts by 12 you can find the amount of tips used in a given simulation. Note the tip_rack_count property has no ceiling at this point so it may no be the same as the calculated integer 

We can set the max counts by doing the following
```
TrackObject.max_rack_count[rackName] = int
```

### Troubleshooting
When setting up our protocol we may want to track what the tracker is doing when protocols are failing or we may or may not want the protocol to print comments to the user about its actions. We can do the following with a couple of arguments when defining the TrackerObject

```
TrackerObject = TipTracker(
		protocol_context=ctx,
		pipette1=single_50, 
		pipette2=multi_50,
		waste_bin=chute,
		use_gripper=use_gripper = True,
		debugging=True,
		suppress_comments=True
)
```

Setting debugging to True will print its actions as print commands and is useful for checking to make sure the right pipette is being used at a given time or the deck is resetting when you expect it (uses `print()` commands). Setting suppress_comments to True will remove the those same comments from being displayed to the user during RunTime.

You can also `print(TrackerObject.pick_up_tip())` to see what was needed for a given tip pick up. Right now this returns an integer corresponding to the motions needed to pick up the tip.


Thats the basics! Keep assigning tips as necessary and the protocol will automatically move tipracks around as needed and also pause if it doesn't have enough. 
