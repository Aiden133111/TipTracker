from opentrons import protocol_api, types
import math
from opentrons.protocol_api import ParameterContext, ALL, COLUMN, SINGLE, ROW
from opentrons.protocol_api.labware import OutOfTipsError

#Currently an error for 2 batches,l ack of mix vol in resuspend beads causes no mixing - fix implemented not put onto library
# Fragment time for RTP 5-10 minutes
# Ethanol tips reduce the amount of tips for ethanol washes

#DO NOT TOUCH THIS - EMAIL AIDEN FOR ASSISTANCE IN TIPTRACKER
class TipTracker:
	'''Create a tip tracking object to easily facitate how protocols that require many tips should have them added to the deck. \
		Will pause the protocol to refill tips when empty. Or will move extra tipracks from expansion slots to the active deck when out \
		Can also dispose of emopty racks through with the gripper and waste chute if both are connected to the robot.
		Track_object = TipTracker(ctx, pipette1, pipette2, waste_bin, use_gripper=False)
		ctx = protocol_api.ProtocolContext , your protocol context to access protocol information
		pipette1 = protocol_api.InstrumentContext , your first pipette
		pipette2 = protocol_api.InstrumentContext , your second pipette 
		waste_bin = protocol_api.WasteChute or protocol_api.TrashBin , the waste being used
		use_gripper = bool, if True will use the gripper to move labware, if False will use the manual method of moving labware off and on deck. \
		'''
	#Off deck type name as str OffDeckType.OFF_DECK

	def __init__(self, ctx : protocol_api.ProtocolContext, pipette1 : protocol_api.InstrumentContext, pipette2 : protocol_api.InstrumentContext, waste_bin : protocol_api.WasteChute | protocol_api.TrashBin, use_gripper : bool = False, debugging : bool = False, suppress_comments : bool = False):

		self.ctx : protocol_api.ProtocolContext = ctx													#ProtocolContext
		self.debug : bool = debugging																	#Debugging mode flag
		self.pipette1 : protocol_api.InstrumentContext = pipette1										#First pipette
		self.pipette2 : protocol_api.InstrumentContext | None = pipette2								#Second Pipette
		self.ex_slots : list[str] | None = []															#If using expansion slots
		self.use_gripper : bool = use_gripper															#If using gripper
		self.waste : protocol_api.WasteChute | protocol_api.TrashBin = waste_bin						#The waste bin type to use
		self.tipracks : dict[protocol_api.Labware.load_name : list[str]] = {}							#Active deck tiprack tracker, internal strings are deck slots
		self.ex_racks : dict[protocol_api.Labware.load_name : list[str]] = {}							#Expansion slots tiprack tracker
		self.empty_ex_slots : dict[protocol_api.Labware.load_name : list[str]] = {}						#Dictionary of empty expansion slots that previously had tips
		self.rack_assignments : dict[protocol_api.Labware.load_name : list[str]] = {}					#Dictionary map of where tipracks should be loaded
		self.tip_counts : dict[protocol_api.Labware.load_name : int] = {}								#Dictionary of # of used tips for each rack type 
		self.tip_rack_counts : dict[protocol_api.Labware.load_name : int] = {}							#Dictionary of tipracks loaded for each rack type
		self.open_slot : str | None = None																#Slot with nothing on it, placeholder slot for carousel
		self.original_open_slot : str | None = None														#Origional open_slot for carousel
		self.stackers : dict[protocol_api.Labware.load_name : list[list[protocol_api.ModuleContext,int,bool]]] = {}#Dictionary of stacker instrument context and number of racks in the stacker, key is rack load name
		self.use_chute : bool = True if type(waste_bin) == protocol_api.WasteChute else False			#Use waste chute to dispose of tips if present 
		self.carousel_tips : bool = False if type(waste_bin) == protocol_api.WasteChute else True		#Carousel tips if no waste chute
		self.pick_up_count : dict[protocol_api.InstrumentContext : int] = {pipette1 : 0, pipette2 : 0} 	#How many time pick up tip has been called for each pipette
		self.drop_count : dict[protocol_api.InstrumentContext : int] = {pipette1 : 0, pipette2 : 0}		#How many time drop tip has been called for each pipette
		self.print_comments : bool = not suppress_comments 												#If True, will print comments to the protocol log
		self.max_racks_count : dict = {}
		self.ignore_slots : list[str] = []
		self.pick_up_slots = {}																			#A dictionary of tiprack load names and the slots they should only pick up from to force a pickup in a given slot, useful for partial tip pickups
		self.active_pipette = None																		#Currently active pipette for pick up if not specified				
		self.return_to_stacker : bool = False															#If something has been moved from the shuttle and needs to be returned during a tip replacement


	def assign_slots(self, tiprack1 : str, slots1 : str | list[str], tiprack2 : str = None,slots2 : list[str] | str = None, tiprack3 : str = None, slots3 : str | list[str] = None):
		'''Dedicate slots to a tiprack, this is used as the slots to refill racks on the deck when they are out. \
			Use this method when the slots you want tips to be reloaded on are different than the slots they started on. \
			This is currently UNTESTED to reuse during a protocol. Can take three tipracks-slot pairs at once.
			tiprack1 = str of the tiprack load name,
			slots1 = list of slots to load tiprack1 onto, can be str or list of strings
			tiprack2 = str of the tiprack load name,
			slots2 = list of slots to load tiprack2 onto, can be str or list of strings
			tiprack3 = str of the tiprack load name,
			slots3 = list of slots to load tiprack3 onto, can be str or list of strings'''
		for tiprack,slots in zip([tiprack1, tiprack2, tiprack3],[slots1, slots2, slots3]):
			if tiprack == None and slots == None:
				continue
			if type(slots) == str:
				slots = [slots]
			self.rack_assignments[tiprack] = slots

	def load_tipracks(self, tiprack1 : str, slots1 : str | list[str], tiprack2 : str = None,slots2 : list[str] | str = None, tiprack3 : str = None, slots3 : str | list[str] = None):
		'''Add tipracks to the deck and to the internal data. This method should always be used to load tipracks onto the deck to change decklayout. \
			Use starting tipracks to intially load the deck to also assign the same slots to the tipracks\
			Although it is done manually through the code when more tipracks are needed, it can be used independently to load tipracks onto the deck. \
			Can take three tipracks-slot pairs at once. This done initially when calling starting_tipracks.
			tiprack1 = str of the tiprack load name,
			slots1 = list of slots to load tiprack1 onto, can be str or list of strings
			tiprack2 = str of the tiprack load name,
			slots2 = list of slots to load tiprack2 onto, can be str or list of strings
			tiprack3 = str of the tiprack load name,
			slots3 = list of slots to load tiprack3 onto, can be str or list of strings'''
		for slots in [slots1, slots2, slots3]:
			if type(slots) == str:
				slots = [slots]

		#Load labware for each tiprack in each slot
		for rackname,slots in zip([tiprack1, tiprack2, tiprack3],[slots1, slots2, slots3]):
			if rackname != None:
				for slot in slots:
					if self.max_racks_count.get(rackname,None) != None:
						if self.max_racks_count[rackname] == self.tip_rack_counts.get(rackname,0):
							if self.print_comments:
								self.ctx.comment(f'Max racks of {rackname} reached, not loading more')
							if self.debug:
								print(f'Max racks of {rackname} reached, not loading more')
							continue
					if type(slot) == str:
						rack = self.ctx.load_labware(rackname, slot)
						if rackname not in self.tip_rack_counts.keys():
							self.tip_rack_counts[rackname] = 1
						else:
							self.tip_rack_counts[rackname] = self.tip_rack_counts[rackname] + 1
					else:
						raise TypeError('This class cannot handle tipracks on adapter. 96 channel tip tracking not supported')
					if self.ex_slots != None and slot in self.ex_slots:
						if rackname in self.ex_racks.keys():
							self.ex_racks[rackname].append(rack)
						else:
							self.ex_racks[rackname] = [rack]
					else:
						if rackname in self.tipracks.keys():
							self.tipracks[rackname].append(rack)
						else:
							self.tipracks[rackname] = [rack]

	def pick_up(self, pipette : int | str | protocol_api.InstrumentContext | None = None, locus : protocol_api.Labware | protocol_api.Well | None = None, refill_all : bool = False, set_active_pipette : bool = False) -> int:
		'''Main use of the tracker function. If we run out of tips using this method, instead of an error being thrown it will check for extra racks in expansion slots \
		or prompt users to phyically refill the tips. It will use the waste chute to throw out the empty tip racks before it needs to refill.
		pipette = Any of the following (1,'One','one','1') or (2,'2',self.pipette2,'two','Two') to indicate which pipette to use
		locus = optional Labware or Well to use to pick up tip, for example reuse tips
		refill_all = bool, if True will refill all other empty racks with tips when out of the needed tip, if False will only refill the assigned tipracks that are out
		
		Returns Integer corresponding to the following:
		0 - Just Pickup, succesful pickup, no swap needed
		1 - Had to carousel to pickup tip
		2 - Wasted Tip, Grabbed from expansion
		3 - Wasted Tip, Grabbed from stacker
		4 - Manual Refill started
		'''
		#Assign proper pipette and check what tips are currently assigned
		if pipette != None:
			active_pipette = self.pipette1 if pipette in (1,'1',self.pipette1,'one','One') else self.pipette2 if pipette in (2,'2',self.pipette2,'two','Two') else None
			if set_active_pipette:
				self.active_pipette = active_pipette
		elif pipette == None and self.active_pipette != None:
			active_pipette = self.active_pipette
		if active_pipette == None and self.active_pipette == None:
			raise ValueError(f"Invalid pipette: {pipette}, must be in [1,'1',self.pipette1,'one','One'] or [2,'2',self.pipette2,'two','Two']")
		self.pick_up_count[active_pipette] = self.pick_up_count[active_pipette] + 1
		#update tiprack list if deck has changed since last pick up
		rack_name = active_pipette.tip_racks[0].load_name
		active_pipette.tip_racks = self.tipracks[rack_name]
		old_rack_slots = [slot for slot in self.rack_assignments[rack_name] if slot not in self.ignore_slots] # Get the slots that are not expansion slots
		waste_slots = [slot for slot in old_rack_slots if slot not in self.ex_slots and slot not in self.ignore_slots] # Get the slots that are not expansion slots or ignored slots
		#Add rack slots to a dictionary IFF they have no tips
		other_rack_slots = { rack_load_name : [rack.parent for rack in rack_list if not any([well.has_tip for well in rack.wells()])] for rack_load_name,rack_list in self.tipracks.items() if rack_load_name != rack_name} # Move these to waste
		empty_tip_slots = {rack_load_name : [slot for slot in racklist if self.ctx.deck[slot] == None] for rack_load_name, racklist in self.rack_assignments.items()} # Load these plus other racks slots
		
		if self.open_slot != None and self.original_open_slot == None:
			self.original_open_slot = self.open_slot
		#Try and pick up tip
		
		#IF this rack should only be on the slot
		if rack_name in self.pick_up_slots.keys():
			#Check if tiprack has tips first
			next_tip = self.ctx.deck[self.pick_up_slots[rack_name]].next_tip()
			if next_tip == None:
				if self.debug:
					print(f'No Tips availalble for pickup on slot {self.pick_up_slots[rack_name]}, shuffling tipracks')
				if self.print_comments:
					self.ctx.comment(f'No Tips availalble for pickup on slot {self.pick_up_slots[rack_name]}, shuffling tipracks')
				if self.pick_up_slots.get(rack_name,None) != None:
					self.shuffle_for_forced_pickup(rack_name,self.pick_up_slots[rack_name], active_pipette)

		try:
			active_pipette.pick_up_tip(locus)
			return_code =  0
		except Exception:
			print('Active Channels',active_pipette.active_channels)
			if self.print_comments:
				self.ctx.comment('Out of tips, starting refilling process')
			if self.debug:
				print('Out of tips, starting refilling process')
			#Trash old tips
			if not self.carousel_tips: #Trash tips in waste chute if able
				for slot in waste_slots:
					if self.ctx.deck[slot] != None and self.ctx.deck[slot].load_name == rack_name:
						self.waste_tips(slot)
			#If out of tips and no expansions, refill tips of the same size
			if self.ex_racks.get(rack_name, None) == None and self.stackers.get(rack_name,None) == None:
				if self.print_comments:
					self.ctx.comment('No expansion slots / stackers defined, Refilling Manually') # Dont have to worry about carousel here, no ex slots
				if self.debug:
					print('No expansion slots / stackers defined, Refilling Manually')
				self.refill_tips(rack_name,old_rack_slots)
				self.ctx.home()
				self.ctx.pause(f"Please place {rack_name} onto slots {old_rack_slots}")
				self.assign_tipracks(active_pipette,rack_name)
				#Optionally refill all used tip racks, dont think this counts expansion deck slots
				if refill_all:
					if self.print_comments:
						self.ctx.comment('Refilling all other tips')
					if self.debug:
						print('Refilling all other tips')
					for other_rack_names,other_slots in other_rack_slots.items():
						if other_slots != [] or empty_tip_slots[other_rack_names] != []:
							if other_slots != []:
								self.waste_tips(other_slots)
							self.ctx.home()
							self.ctx.pause(f"Please place {other_rack_names} onto slots {other_slots + empty_tip_slots[other_rack_names]}")
							self.refill_tips(other_rack_names,other_slots + empty_tip_slots[other_rack_names])
				active_pipette.pick_up_tip(locus)
				return_code = 4
			else:
				if self.print_comments:			
					self.ctx.comment('Expansion slots or stackers defined, starting refilling process')
				if self.debug:
					print('Expansion slots or stackers defined, starting refilling process')
				if refill_all:
					if self.print_comments:
						self.ctx.comment('Refilling all other tips')
					if self.debug:
						print('Refilling all other tips')
					for other_rack_names,other_slots in other_rack_slots.items():
						if other_slots != [] or empty_tip_slots[other_rack_names] != []:
							if other_slots != []:
								self.waste_tips(other_slots)
							self.ctx.home()
							self.ctx.pause(f"Please place {other_rack_names} onto slots {other_slots + empty_tip_slots[other_rack_names]}")
							self.refill_tips(other_rack_names,other_slots + empty_tip_slots[other_rack_names])
				if rack_name in self.ex_racks.keys() and self.ex_racks[rack_name] != []:
					if self.print_comments:
						self.ctx.comment('Tiprack on expansion slot, moving to active deck')
					if self.debug:
						print('Tiprack on expansion slot, moving to active deck')
					if self.carousel_tips:
						for old_rack,e_rack in zip(self.tipracks[rack_name],self.ex_racks[rack_name]):
							self.carousel(old_rack,e_rack)
							return_code = 1
					else:
						for e_rack, open_slot in zip(self.ex_racks[rack_name],waste_slots): #This needs a check for if expansion slot has tips 
							e_slot_source = e_rack.parent
							self.ctx.move_labware(e_rack, open_slot,use_gripper=self.use_gripper)
							if rack_name in self.empty_ex_slots.keys():
								self.empty_ex_slots[rack_name].append(e_slot_source)
							else:
								self.empty_ex_slots[rack_name] = [e_slot_source]
							return_code = 2
					self.reset_rack_list(rack_name)			
					self.assign_tipracks(active_pipette,rack_name)
					
					active_pipette.pick_up_tip(locus)
				elif rack_name in self.stackers.keys() and sum([stacker[1] for stacker in self.stackers[rack_name]]) > 0:
					print(sum([stacker[1] for stacker in self.stackers[rack_name]]))
					if not self.carousel_tips:
						for slot in waste_slots:
							for stacker in self.stackers[rack_name]:
								if stacker[1] > 0:
									if self.print_comments:
										self.ctx.comment(f'Tiprack in {stacker[0]}, moving to {slot}')
									if self.debug:
										print(f'Tiprack in {stacker[0]}, moving to {slot}')
									next_rack = self.move_from_stacker(rack_name)
									self._shuttle_labware(next_rack,slot)
									break
								else:
									if self.print_comments:
										self.ctx.comment(f'No remaining tipracks in {stacker[0]}')
									if self.debug:
										print(f'No remaining tipracks in {stacker[0]}')
					else:
						if self.print_comments:
							self.ctx.comment('Tiprack in stacker, carouseling to active deck')
						if self.debug:
							print('Tiprack in stacker, carouseling to active deck')
						for old_rack in self.tipracks[rack_name]:
							for stacker in self.stackers[rack_name]:
								if stacker[1] > 0:
									next_rack = self.move_from_stacker(rack_name)
									self.carousel(old_rack,next_rack)
									break
								else:
									if self.print_comments:
										self.ctx.comment(f'No remaining tipracks in {stacker[0]}')
									if self.debug:
										print(f'No remaining tipracks in {stacker[0]}')
					self.reset_rack_list(rack_name)
					self.assign_tipracks(active_pipette,rack_name)
					active_pipette.pick_up_tip(locus)
					return_code = 3
				else:
					call_refill = True
					if rack_name in self.stackers.keys():
						if self.print_comments:
							self.ctx.comment('No remaining tipracks in stackers, manual refill needed')
						if self.debug:
							print('No remaining tipracks in stackers, manual refill needed')
						for x,stacker in enumerate(self.stackers[rack_name]):
							count_to_load = 6 if self.max_racks_count.get(rack_name,None) == None else min(6,self.max_racks_count[rack_name]-self.tip_rack_counts.get(rack_name,0))
							self.stackers[rack_name][x][1] = count_to_load
							lid_load_name = 'opentrons_flex_tiprack_lid' if self.stackers[rack_name][x][2] else None
							self.load_tips_in_stacker(self.stackers[rack_name][x][0],rack_name,count_to_load,lid_load_name,True if count_to_load > 6 else False) #hard coded this to never place on shuttle, revist later to give flexibility
							stacker_message = f'Please load {count_to_load - 1 if count_to_load > 6 else count_to_load} {rack_name} into {stacker[0]}'
							if count_to_load > 6:
								stacker_message = stacker_message + ' and place one on the shuttle'
							self.ctx.pause(stacker_message)
						if self.max_racks_count.get(rack_name,None) == self.tip_rack_counts.get(rack_name,-1):
							call_refill = False
							if self.print_comments:
								self.ctx.comment(f'Max racks of {rack_name} reached, last racks in stacker')
							if self.debug:
								print(f'Max racks of {rack_name} reached, last racks in stacker')
							for empty_slot in old_rack_slots[:count_to_load]:
								next_rack = self.move_from_stacker(rack_name)
								self._shuttle_labware(next_rack,empty_slot)
					if rack_name in self.ex_racks.keys() and call_refill:
						if self.print_comments:
							self.ctx.comment('No remaining tipracks on expansion deck, manual refill needed')
						if self.debug:
							print('No remaining tipracks on expansion deck, manual refill needed')
						
						#CHeck here for empty tipracks on deck
					self.ctx.home()
					print(self.stackers)
					if call_refill:
						if old_rack_slots != []:
							self.ctx.pause(f'Place {rack_name} onto slots {old_rack_slots}')
						self.refill_tips(rack_name,self.rack_assignments[rack_name])
					print(self.stackers)
					self.reset_rack_list(rack_name)
					print(self.stackers)
					self.assign_tipracks(active_pipette,rack_name)
					self.open_slot = self.original_open_slot
					active_pipette.pick_up_tip(locus)
					return_code =  4
					
					#Pause protocol and prompt user to load new tipracks, could we have option to add all tipracks
		if self.return_to_stacker:
			if self.print_comments:
				self.ctx.comment('Returning labware to stacker')
			if self.debug:
				print('Returning labware to stacker')
			stacker_origional_labware, holding_slot, rackname, chosen_index  = self.return_to_stacker
			self._shuttle_labware(stacker_origional_labware,self.stackers[rackname][chosen_index][0])
			self.return_to_stacker = False
		if rack_name in self.tip_counts.keys():
			self.tip_counts[rack_name] = self.tip_counts[rack_name] + active_pipette.active_channels
		else:
			self.tip_counts[rack_name] = active_pipette.active_channels
		return return_code
	
	def shuffle_for_forced_pickup(self, rack_name : str, pick_up_slot : str, pipette : protocol_api.InstrumentContext):
		#TO DO, FIND NEXT TIPRACK with TIPS, MOVE OLD RACK AWAY, NEXT RACK IN
		empty_rack = self.ctx.deck[pick_up_slot]
		for slot in self.rack_assignments[rack_name]:
			print(slot)
			if slot == pick_up_slot or self.ctx.deck[slot] == None:
				continue
			elif self.ctx.deck[slot]:
				next_rack = self.ctx.deck[slot]
		if self.carousel_tips:
			self.carousel(empty_rack,next_rack)
			self.reset_rack_list(rack_name)
			self.assign_tipracks(pipette,rack_name)
		elif self.use_chute:
			if self.print_comments:
				self.ctx.comment(f'Disposing of empty tiprack in {pick_up_slot} replacing with {next_rack.parent}')
			if self.debug:
				print(f'Disposing of empty tiprack in {pick_up_slot} replacing with {next_rack.parent}')
			self.ctx.move_labware(empty_rack,self.waste,use_gripper=self.use_gripper)
			self.ctx.move_labware(next_rack,self.pick_up_slots[rack_name],use_gripper=self.use_gripper)
			self.reset_rack_list(rack_name)
			self.assign_tipracks(pipette,rack_name)

	def add_starting_tipracks(self, tiprack1 : str, slots1 : str | list[str], tiprack2 : str = None,slots2 : list[str] | str = None, tiprack3 : str = None, slots3 : str | list[str] = None, max_racks_1 : int = None, max_racks_2 : int = None, max_racks_3 : int = None):
		'''Load tipracks onto the deck and assign the proper slots to reload them onto. This method should always be used to the first set of tipracks just to ensure they properly \
			match, but these variables could be different. i.e. you do not want the tipracks to be refilled onto the same slots as they start on. Can take three tipracks-slot pairs at once.
			tiprack1 = str of the tiprack load name,
			slots1 = list of slots to load tiprack1 onto, can be str or list of strings
			tiprack2 = str of the tiprack load name,
			slots2 = list of slots to load tiprack2 onto, can be str or list of strings
			tiprack3 = str of the tiprack load name,
			slots3 = list of slots to load tiprack3 onto, can be str or list of strings'''
		assign_slots = [slots1, slots2, slots3]
		tipracks = [tiprack1, tiprack2, tiprack3]
		for slot, rack in zip(assign_slots,tipracks):
			if slot != None and rack != None:
				continue
			elif slot == None and rack == None:
				continue
			else:
				raise ValueError(f"Tiprack {rack} and slots {slot} must be defined together")
			
		for max_rack,tiprack in zip([max_racks_1, max_racks_2, max_racks_3],tipracks):
			if max_rack != None and type(max_rack) != int:
				raise TypeError(f"Max racks must be an integer, got {type(max_rack)}")
			else:
				if max_rack != None:
					self.max_racks_count[tiprack] = max_rack
		self.load_tipracks(tiprack1,slots1,tiprack2,slots2,tiprack3,slots3)
		self.assign_slots(tiprack1,slots1,tiprack2,slots2,tiprack3,slots3)

	def reset_rack_list(self,rack_name):
		'''Fetches a rackname and resets its internal data for the type of rack. Can be useful when you move the deck around with the gripper, \
		place new racks on the deck or even perform manual moves of labware 
		rack_name = str of the rack load name to reset, i.e. opentrons_flex_96_tiprack_50ul'''
		rack_list = []
		ex_list = []
		for slot,item in self.ctx.deck.items(): 
			if not item or item in self.ctx.loaded_modules.values():
				continue
			if item.load_name == 'opentrons_flex_96_tiprack_adapter':
				continue
			else:
				rack_obj = item
			if rack_obj.load_name == rack_name:
				if slot in self.ex_slots:
					ex_list.append(rack_obj)
				else:
					rack_list.append(rack_obj)
		self.tipracks[rack_name] = rack_list
		self.ex_racks[rack_name] = ex_list


	def add_expansion_slots(self, slots):
		'''Add expansion slots to the available slots on the deck.
		 This will not be used unless you assign them to a tiprack as well to load tipracks onto  the deck slot
		 slots = ['A4','B4','C4','D4'] as list of strings or 'A4' as string \
		'''
		if self.ex_slots == None:
			if type(slots) == str:
				self.ex_slots = [slots]
			elif type(slots) == list:
				self.ex_slots = slots
			else:
				raise TypeError("Expansion slots must be a string or list of strings")
		else:
			if type(slots) == str:
				self.ex_slots.append(slots)
			elif type(slots) == list:
				self.ex_slots.extend(slots)
			else:
				raise TypeError("Expansion slots must be a string or list of strings")
		self.ex_slots = list(set(self.ex_slots))
		invalid_slots = [x for x in self.ex_slots if x not in ['A4','B4','C4','D4']]
		if len(invalid_slots) > 0:
			raise ValueError(f"Invalid expansion slots: {invalid_slots}, slots must be A4, B4, C4, or D4")
			
	def drop_tip(self, pipette : int | str | protocol_api.InstrumentContext = None, locus : protocol_api.Labware | protocol_api.Well | None = None, return_tip : bool = False):
		'''Drop tip at locus, if locus is None will drop tip at the default waste bin if dropping or back to its original slot if returning. 
		pipette = 1 or 2, corresponding to which order you loaded them in
		locus = labware or well to drop tip at, if None will drop at default waste bin
		return_tip = bool, if True will return tip to original slot instead of dropping it at the waste bin'''
		if pipette != None:
			pip = self.pipette1 if pipette in (1,'1',self.pipette1,'one','One') else self.pipette2 if pipette in (2,'2',self.pipette2,'two','Two') else None
		elif pipette == None and self.active_pipette != None:
			pip = self.active_pipette
		if pip == None and self.active_pipette == None:
			raise ValueError(f"Invalid pipette number {pipette}, must be 1 or 2, as strings or integers or pipette objects")
		self.drop_count[pip] = self.drop_count[pip] + 1
		if return_tip:
			pip.return_tip(locus)
		else:
			pip.drop_tip(locus)

			
	def replace_tips(self,old_rack_name : str, new_rack_name : str , number_to_replace : int | None = None, manually_remove = True):
		'''Remove a certain number (or all) of a certain type of tiprack to replace with a new type. \
		Useful when you no longer need a type of tip on deck and you want the space for something else.
		old_racks = list of tiprack labware objects to replace, can be a list of labware or a single labware object
		new_rack_name = str of the new tiprack load name
		number_to_replace = int of how many to replace, if None will replace all of that type'''
		if self.print_comments:
			self.ctx.comment(f'Replacing {number_to_replace} {old_rack_name} with {new_rack_name}')
		if self.debug:
			print('Replacing {number_to_replace} {old_rack_name} with {new_rack_name}')
		slot_list = self.rack_assignments[old_rack_name][:number_to_replace]
		if self.print_comments:
			self.ctx.comment('Replacing tipracks')
		if self.debug:
			print('Replacing Tipracks')
		self.ctx.home()
		self.clear_old(old_rack_name,slot_list,manually_remove)
		new_rack_slot_list = self.rack_assignments[new_rack_name].extend(slot_list)
		old_rack_slot_list = [] if number_to_replace == None else self.rack_assignments[old_rack_name][self.rack_assignments[old_rack_name][number_to_replace:]]
		#Option to remove the old assignment 
		self.assign_slots(tiprack1=new_rack_name,slots1=new_rack_slot_list,
						tiprack2=old_rack_name,slots2=old_rack_slot_list)
		self.load_tipracks(new_rack_name,slot_list)


	def refill_tips(self, name, slots):
		'''Internal Function to facilitate refilling tips of the same size. First clears the old data then replaces it by loading fresh tip boxes. \
		Can call this method manually instead of calling clear_old load_tipracks independently. \
		name = tiprack load name as str
		slots = list of slots to refill, if str or labware will be converted to list. Labware should be passed only if it is a tiprack adapter'''
		if self.ignore_slots != []:
			slots = [slot for slot in slots if slot not in self.ignore_slots]
			if self.print_comments:
				self.ctx.comment(f'Ignoring slots {self.ignore_slots} for refill')
			if self.debug:
				print(f'Ignoring slots {self.ignore_slots} for refill')
		if self.print_comments:
			self.ctx.comment(f'Refilling tips of {name} on {slots}')
		if self.debug:
			print(f'Refilling tips of {name} on {slots}')
		self.clear_old(name,None,False)
		
		self.load_tipracks(name,slots)

	def waste_tips(self, slots):
		'''Move tipboxes to waste, this is done automatically when a all types of a tip are used or when refill_all=True for tip pickup but can be used to manually move tips from any slots to waste. \
		If called manually, make sure to use clear_old on the slots after to remove it from internal data \
		slots = list of slots to move to waste, if str or labware will be converted to list. Labware should be passed only if it is a tiprack adapter'''
		if self.print_comments:
			self.ctx.comment(f'Wasting tips on slots {slots}: Using gripper : {self.use_gripper}')
		if self.debug:
			print(f'Wasting tips on slots {slots}: Using gripper : {self.use_gripper}')
		if type(slots) == str or type(slots) == protocol_api.Labware:
			slots = [slots]
		if self.use_chute and self.use_gripper:
			for slot in slots:
				if slot in self.ignore_slots:
					if self.debug:
						print(f'Ignoring slot {slot} for waste tips')
					continue
				self.ctx.move_labware(self.ctx.deck[slot], self.waste,use_gripper=self.use_gripper)
		else:
			for slot in slots:
				if slot in self.ignore_slots:
					if self.debug:
						print(f'Ignoring slot {slot} for waste tips')
					continue
				self.ctx.move_labware(self.ctx.deck[slot], protocol_api.OFF_DECK)

	def assign_tipracks(self, pipette : int | str | protocol_api.InstrumentContext, name : str):
		'''Assign tipracks to pipette, this is done automatically when loading tips but can be used to reassign if needed.\
		Instead of pip.tip_racks = [tipracks], use trackerObj.assign_tipracks(1,opentrons_flex_96_filtertip_50ul).\
		pipette = 1 or 2, corresponding to which order you loaded them in
		name = tiprack load name as str'''
		if self.print_comments:
			self.ctx.comment(f'Reassigning tipracks of {pipette} to {name}')
		if self.debug:
			print(f'Reassigning tipracks of {pipette} to {name}')
		pip = self.pipette1 if pipette in (1,'1',self.pipette1,'one','One') else self.pipette2 if pipette in (2,'2',self.pipette2,'two','Two') else None
		if pip == None:
			raise ValueError(f"Invalid pipette number {pipette}, must be 1 or 2")
		pip.tip_racks = self.tipracks[name]

	def clear_old(self,name : str ,slots_to_clear : None | list = None,save_tips = True, waste_expansion : bool = False):
		'''Remove old tipracks from internal data to replace with new tipracks in another function. This should generally only be used internally.\
		Only use if you are sure you want to remove the tipracks from the internal data without moving them off deck physically. Keeps protocol from trying to move labware not on the deck anymore
		name = Tiprack load name
		slots_to_clear = List of slots to clear, if None will clear all tipracks of that type'''		
		if self.print_comments:
			self.ctx.comment(f'Clearing old tipracks of {name}')
		if self.debug:
			print(f'Clearing old tipracks of {name}')
		print(self.stackers)
		if save_tips == False and self.use_chute == True and self.use_gripper == True:
			if self.print_comments:
				self.ctx.comment('Using Gripper to remove tips')
			if self.debug:
				print('Using Gripper to remove tips')
			toss_tips = True
			toss_location = self.waste
		else:
			slots_message = 'All slots' if slots_to_clear == None else str(slots_to_clear)
			self.ctx.pause(f'Please remove all {name} from {slots_message}')
			toss_tips = False
			toss_location = protocol_api.OFF_DECK
		if slots_to_clear == None:
			slots_to_clear = [slot for slot in self.rack_assignments.get(name,[]) if self.ctx.deck[slot] != None and slot not in self.ignore_slots]
			for slot in slots_to_clear:
				rack = self.ctx.deck[slot]
				if rack in self.ctx.loaded_modules.values():
					continue #prevent things on the stacker from being moved incorrectly when nothing is not the third column
				self.ctx._core.move_labware(
						labware_core=rack._core,
						new_location=toss_location,
						use_gripper=toss_tips,
						pause_for_manual_move=False,
						pick_up_offset=(0.0,0.0,0.0),
						drop_offset=(0.0,0.0,0.0))
			self.tipracks[name] = []
			self.ex_racks[name] = []
		else:
			pop_these_active_deck = []
			pop_these_expansion_slots = []
			if name in self.tipracks.keys():
				for x,rack in enumerate(self.tipracks[name]):
					if type(rack.parent) == str:
						slot_check = rack.parent
					else:
						slot_check = rack.parent.parent
					if slot_check in slots_to_clear:
						pop_these_active_deck.append(x)
				pop_these_active_deck.sort(reverse=True)
				for x in pop_these_active_deck:
					self.ctx._core.move_labware(
						labware_core=self.tipracks[name][x]._core,
						new_location=toss_location,
						use_gripper=toss_tips,
						pause_for_manual_move=False,
						pick_up_offset=(0.0,0.0,0.0),
						drop_offset=(0.0,0.0,0.0))
					self.tipracks[name].pop(x)
				for x,rack in enumerate(self.ex_racks[name]):
					if type(rack.parent) == str:
						slot_check = rack.parent
					else:
						slot_check = rack.parent.parent
					
					if slot_check in slots_to_clear:
						pop_these_expansion_slots.append(x)
				pop_these_expansion_slots.sort(reverse=True)
				for x in pop_these_expansion_slots:
					self.ctx._core.move_labware(
						labware_core=self.ex_racks[name][x]._core,
						new_location=toss_location,
						use_gripper=toss_tips,
						pause_for_manual_move=False,
						pick_up_offset=(0.0,0.0,0.0),
						drop_offset=(0.0,0.0,0.0))
					self.ex_racks[name].pop(x)
			else:
				raise KeyError(f"Tiprack {name} not found in tiprack list")
		print('After clear old',self.stackers)
			
	def carousel(self, tiprack_to_move_away : protocol_api.Labware | str,tiprack_to_move_in : protocol_api.Labware | str):
		
		if self.open_slot != None:
			open_slot = self.open_slot
		else:
			raise ValueError("No open slot defined, please define an open slot to move the tiprack to")
		
		if type(tiprack_to_move_away) == str:
			intermediate_slot = tiprack_to_move_away
			tiprack_to_move_away = self.ctx.deck[intermediate_slot]
		elif type(tiprack_to_move_away) == protocol_api.Labware:
			intermediate_slot = tiprack_to_move_away.parent
		
		if type(tiprack_to_move_in) == str:
			leaving_open_slot = tiprack_to_move_in
			tiprack_to_move_in = self.ctx.deck[leaving_open_slot]
		elif type(tiprack_to_move_in) == protocol_api.Labware:
			leaving_open_slot = tiprack_to_move_in.parent
		
		
		if self.debug:
			print(f' Carousel from {tiprack_to_move_away} on {intermediate_slot} to {self.open_slot}')
		self._shuttle_labware(tiprack_to_move_away,open_slot)
		if self.debug:
			print(f' Carousel from {tiprack_to_move_in} on {leaving_open_slot} to {intermediate_slot}')
		self._shuttle_labware(tiprack_to_move_in,intermediate_slot)
		if self.debug:
			print(f'----->Assigning open_slot to {leaving_open_slot}')
		self.open_slot = leaving_open_slot
				
	def move_from_stacker(self,rackname,):
		for x,stacker_list in enumerate(self.stackers[rackname]):
			if stacker_list[1] > 0:
				stacker : protocol_api.FlexStackerContext = stacker_list[0]
				chosen_index = x
				break
		stacker_current_labware = stacker.labware
		if stacker_current_labware == None:
			if self.print_comments:
				self.ctx.comment(f'Retrieving labware from stacker for {rackname}')
			if self.debug:
				print(f'Retrieving labware from stacker for {rackname}')
			labware = stacker.retrieve()
			self.stackers[rackname][chosen_index][1] = self.stackers[rackname][chosen_index][1] - 1 #Change Quantity of stacker
			if self.stackers[rackname][chosen_index][2]: #This should be changes, internal flag for lid
				if self.print_comments:
					self.ctx.comment(f'Removing lid from stacker for {rackname}')
				if self.debug:
					print(f'Removing lid from stacker for {rackname}')
				self.ctx.move_lid(labware,self.waste,use_gripper=self.use_gripper)
		
		else:
			if stacker_current_labware.load_name != rackname:
				if self.print_comments:
					self.ctx.comment(f'Labware on stacker is not {rackname}, moving to {self.open_slot} and retrieving new labware')
				if self.debug:
					print(f'Labware on stacker is not {rackname}, moving to {self.open_slot} and retrieving new labware')
				self._shuttle_labware(stacker_current_labware,self.open_slot)
				self.return_to_stacker = (stacker_current_labware,self.open_slot,rackname,chosen_index)
				labware = stacker.retrieve()
				self.stackers[rackname][chosen_index][1] = self.stackers[rackname][chosen_index][1] - 1 #Change Quantity of stacker
				if self.stackers[rackname][chosen_index][2]: 
					if self.print_comments:
						self.ctx.comment(f'Removing lid from stacker for {rackname}')
					if self.debug:
						print(f'Removing lid from stacker for {rackname}')
					self.ctx.move_lid(labware,self.waste,use_gripper=self.use_gripper)
			else:
				if self.print_comments:
					self.ctx.comment(f'Getting labware already on shuttle for {rackname}')
				if self.debug:
					print(f'Getting labware already on shuttle for {rackname}')
				labware = stacker_current_labware #If there is a tiprack on the stacker
		return labware
		
	
	def add_stacker(self, slot, rackname, initial_count, lid, load_on_shuttle):
		if self.print_comments:
			self.ctx.comment(f'Adding stacker module on slot {slot} with {initial_count} {rackname}')
		if self.debug:
			print(f'Adding stacker module on slot {slot} with {initial_count} {rackname}')
		stacker_obj = self.ctx.load_module('flexStackerModuleV1', slot)
		if rackname in self.stackers.keys():
			self.stackers[rackname].append([stacker_obj,None,True if lid != None else False])
		else:
			self.stackers[rackname] = [[stacker_obj,None,True if lid != None else False]]
		self.load_tips_in_stacker(stacker_obj,rackname,initial_count,lid,load_on_shuttle)
		return stacker_obj

	def load_tips_in_stacker(self,stacker : protocol_api.FlexStackerContext,rackname : str,quantity : int,lid : str | None = None, load_on_shuttle : bool = True):
		if self.print_comments:
			self.ctx.comment(f'Loading {quantity} {rackname} into stacker in {stacker}')
		if self.debug:
			print(f'Loading {quantity} {rackname} into stacker in {stacker}')
		print(stacker.labware)
		stacker.set_stored_labware(rackname,count=quantity - 1 if load_on_shuttle else quantity,lid=lid)
		print(stacker.labware)
		if rackname not in self.tip_rack_counts.keys():
			self.tip_rack_counts[rackname] = quantity
		else:
			self.tip_rack_counts[rackname] = self.tip_rack_counts[rackname] + quantity
		if load_on_shuttle:
			if self.print_comments:
				self.ctx.comment(f'Loading labware onto stacker shuttle for {rackname}')
			if self.debug:
				print(f'Loading labware onto stacker shuttle for {rackname}')
			stacker.load_labware(rackname)
		for x,stacker_list in enumerate(self.stackers[rackname]):
			if stacker_list[0] == stacker:
				self.stackers[rackname][x][1] = quantity - 1 if load_on_shuttle else quantity

	def _shuttle_labware(self,labware,location):
		#print(self.open_slot)
		self.ctx.move_labware(labware,location,use_gripper=self.use_gripper)

#####################################################################
#START OF ACTUAL PROTOCOL
############PROTOCOL START##############
metadata = {
	'protocolName': 'Illumina Stranded mRNA Prep,Ligation High Throughput',
	'author': 'Opentrons <protocols@opentrons.com>',
	'source': 'Protocol Library',
	}

requirements = {
	"robotType": "Flex",
	"apiLevel": "2.27",
}


MIX_SPEED = 1250 # Speed in RPM for heater shaker, keep under 1500 for full reaction sizes
INCUBATION_TIME = 5 # Time in minutes for bead incubations
ASPIRATE_HEIGHT = 0.6  #Height from bottom to aspirate from during bead cleanups in mm

def add_parameters(parameters : ParameterContext):
	parameters.add_int(
		display_name="Number of Samples",
		variable_name="samples",
		default=8,
		choices=[{"display_name": str(i), "value": i} for i in range(8,97,8)],
		description="Number of samples to process (max 96)"
		)
	parameters.add_int(
		display_name="PCR Cycles",
		variable_name="PCR_Cycles",
		default=12,
		minimum=8,
		maximum=20,
		description="Number of amplification cycles"
		)
	
	parameters.add_int(
		display_name="Column on RNA Anchor Plate",
		variable_name="Index_Anchor_start",
		description="Choose starting column on the RNA Index Anchor plate (for under 96 samples)",
		default=1,
		minimum=1,
		maximum=12,
		)

	parameters.add_int(
		display_name="Column on UD Indexes Plate",
		variable_name="Index_Adapter_start",
		description="Choose starting column on the UD Indexes plate (for under 96 samples)",
		default=1,
		minimum=1,
		maximum=12,
		)

	parameters.add_int(
		display_name="Manual RNA Anchor",
		variable_name="manual_anchor",
		description="Manual : User adds anchors, auto - dilute : add half RSB to anchor, auto - undilute : normal",
		default=2,
		choices=[{"display_name": "Manual", "value": 1}, {"display_name": "Auto - Undilute Anchor", "value": 2},{"display_name": "Auto - Dilute Anchor", "value": 3}]
		)

	parameters.add_float(display_name="Start Step", 
					variable_name='start_step',
					description='What is the first step the protocol. Samples always start in well A1',
					default=0,
					choices=[
						{"display_name": "mRNA Capture", "value": 0},
						{"display_name": "Cleanup cDNA", "value": 1},
						{"display_name": "dA-Tailing", "value": 2},
						{"display_name": "Amplify Library", "value": 3},])
	
	parameters.add_int(display_name="End Step", 
					variable_name='end_step',
					description='After what step should the protocol stop',
					default=3,
					choices=[
						{"display_name": "mRNA Capture", "value": 0},
						{"display_name": "Cleanup cDNA", "value": 1},
						{"display_name": "dA-Tailing", "value": 2},
						{"display_name": "Amplify Library", "value": 3}])
	parameters.add_bool(
		display_name="Foiled Reagent Plate",
		variable_name="foiled_reagent_plate",
		description="When on, protocol will break foil seal on the reagent plate when first accessed",
		default=False
		)
	
	parameters.add_int(
		display_name="Reaction Size",
		variable_name="reaction_size",
		description="What volume ratio to use for each reaction",
		default=1,
		choices=[{"display_name": "Standard", "value": 1}, {"display_name": "Half", "value": 2},{"display_name": "Quarter", "value": 4}]
		)
	
	parameters.add_bool(
		display_name="Pause for Beads",
		variable_name="pause_for_beads",
		description="When on, protocol pauses before bead cleanups allowing user to add beads to reservoir",
		default=False
		)
	
	parameters.add_bool(
		display_name="Bake Off",
		variable_name="bake_off",
		description="Bake Off sets blocks TO 65C for 15 minutes after protocol to dry off condensation",
		default=False
		)
	parameters.add_bool(
		display_name="Dry Run",
		variable_name="DryRun",
		description="Dry runs will skip incubations, thermocycler programs, and return tips",
		default=False
		)
	

#Deck Configuration
def run(ctx : protocol_api.ProtocolContext):
	#Parameters
	DryRun=ctx.params.DryRun
	sample_count= ctx.params.samples
	reaction_size = ctx.params.reaction_size
	Columns = sample_count // 8
	plates_needed = 5
	PCR_Cycles=ctx.params.PCR_Cycles
	Index_Adapter_start=ctx.params.Index_Adapter_start if sample_count < 96 else 1
	Index_Anchor_start=ctx.params.Index_Anchor_start if sample_count < 96 else 1
	start_point = ctx.params.start_step
	stop_point = ctx.params.end_step
	foiled_reagent_plate = ctx.params.foiled_reagent_plate

	####ROBOT SETUP####
	#pipette
	p200 = ctx.load_instrument('flex_96channel_200','left', )
	#Tip Management
	p50_slots = ['C3']
	p200_slots = ['B3']
	TIPS50 = 'opentrons_flex_96_tiprack_50ul'
	TIPS200 = 'opentrons_flex_96_tiprack_200ul'
	
	#hardware
	temp_block = ctx.load_module('temperature module gen2', 'D1')
	temp_adapter = temp_block.load_adapter('opentrons_96_well_aluminum_block')
	heater_shaker = ctx.load_module('heaterShakerModuleV1', 'C1')
	shaker_adapter = heater_shaker.load_adapter('opentrons_96_pcr_adapter')
	stacker_pcr_plate = ctx.load_module('flexStackerModuleV1', 'A4')
	stacker_pcr_plate.set_stored_labware('opentrons_96_wellplate_200ul_pcr_full_skirt',count=plates_needed-1,lid=None)
	thermo = ctx.load_module('thermocycler module gen2')
	mag_block = ctx.load_module('magneticBlockV1', 'D2')
	chute = ctx.load_waste_chute()
	#labware
	reagent_plate = temp_adapter.load_labware('opentrons_96_wellplate_200ul_pcr_full_skirt')
	Reservior = ctx.load_labware('nest_96_wellplate_2ml_deep', 'B2') 
	sample_1 = shaker_adapter.load_labware('opentrons_96_wellplate_200ul_pcr_full_skirt','Sample Plate 1')
	ethanol_reservoir = ctx.load_labware('nest_1_reservoir_290ml', 'C2', 'Ethanol Reservoir')
	adapters = [ctx.load_adapter('opentrons_flex_96_tiprack_adapter', slot) for slot in ['A2','A3']]
	if start_point != 2 and start_point != 3:
		ethanol_removal, ethanol_add = adapters[0].load_labware(TIPS200,'Ethanol Removal TIPS'), adapters[1].load_labware(TIPS200,'Ethanol Addition TIPS')
	else:
		new_50, ethanol_add = adapters[0].load_labware(TIPS50), adapters[1].load_labware(TIPS200,'Ethanol Addition TIPS')
	Index_Anchors = stacker_pcr_plate.load_labware('eppendorf_96_wellplate_150ul_custom','IDT for Illumina Index Anchors') 
	#Tipracks
	tip_manager = TipTracker(ctx,p200,None,chute,True,False,True)
	tip_manager.add_starting_tipracks(TIPS50,p50_slots,TIPS200,p200_slots)
	stacker_50_1 = tip_manager.add_stacker('C4',TIPS50,7,'opentrons_flex_tiprack_lid',True)
	stacker_50_2 = tip_manager.add_stacker('D4',TIPS50,7,'opentrons_flex_tiprack_lid',True)
	if stop_point == 3:
		stacker_200 = tip_manager.add_stacker('B4',TIPS200,6,'opentrons_flex_tiprack_lid',False)
		Index_Plate = stacker_200.load_labware('eppendorf_96_wellplate_150ul_custom','IDT for Illumina DNA/RNA UD Indexes') 
	else:
		stacker_200 = tip_manager.add_stacker('B4',TIPS200,7,'opentrons_flex_tiprack_lid',True)
	p200.flow_rate.blow_out=40

	#Reagent Assignments
	#REAGENT PLATE
	if reaction_size == 1 and Columns > 6:
		FMM	=[reagent_plate['A1'],reagent_plate['A2']] #Fragmentation Master Mix
		ELB= [reagent_plate['A3'],reagent_plate['A4']]
		FSMM	=reagent_plate['A5']
		SMM=[reagent_plate['A6'],reagent_plate['A7']]
		ATL4	=reagent_plate['A8']
		LIGX	=reagent_plate['A9']
		STL	 =reagent_plate['A10']
		EPM	 =[reagent_plate['A11'],reagent_plate['A12']]
		#RESERVIOR
		RPBX=Reservior['A1']
		BWB =[Reservior['A2'],Reservior['A3']]
		BBB=Reservior['A4']
		AMP	 =[Reservior['A5'],Reservior['A6']] # X offset mixing for beads at all steps. For the elution
		RSB=Reservior['A7']
	else:
		FMM	=reagent_plate['A1'] #Fragmentation Master Mix
		ELB=reagent_plate['A2']
		FSMM	=reagent_plate['A3']
		SMM=reagent_plate['A4']
		ATL4	=reagent_plate['A5']
		LIGX	=reagent_plate['A6']
		STL	 =reagent_plate['A7']
		EPM	 =reagent_plate['A8']
		#RESERVIOR
		RPBX=Reservior['A1']
		BWB =Reservior['A2']
		BBB=Reservior['A3']
		AMP	 =Reservior['A4'] # X offset mixing for beads at all steps. For the elution
		RSB=Reservior['A5']

	ETOH	=ethanol_reservoir['A1']

	#VOLUMES

	RPBX_VOL = 25/reaction_size
	BWB_VOL = 100/reaction_size
	BBB_VOL = 25/reaction_size
	AMP_VOL1 = 90/reaction_size
	AMP_VOL2 = 34/reaction_size
	AMP_VOL3 = 40/reaction_size
	ETOH_VOL = 125/reaction_size

	FMM_VOL = 19/reaction_size
	ELB_VOL = 25/reaction_size
	FSMM_VOL = 8/reaction_size
	SMM_VOL = 25/reaction_size
	ATL4_VOL = 12.5/reaction_size
	LIGX_VOL = 2.5/reaction_size
	ANCHOR_VOL = 5/reaction_size
	INDEX_VOL = 10/reaction_size
	STL_VOL = 5/reaction_size
	EPM_VOL = 20/reaction_size

	RSB1_VOL = 19.5/reaction_size
	RSB2_VOL = 22/reaction_size
	RSB3_VOL = 22

	#Plate Assignments
	Anchors=Index_Anchors.rows()[0][Index_Anchor_start-1:12]
	if stop_point == 3:
		Index_Adap=Index_Plate.rows()[0][Index_Adapter_start-1:12]

	def ethanol_wash(wash_pipette : protocol_api.InstrumentContext, wash_buffer : protocol_api.Well, #ONly time dont mix.  Slow aspirate
					wash_volume : float, reagent : protocol_api.Well, reagent_volume : float,
					batch_1 : list[protocol_api.Well], premix_reagent : bool = False,
					reagent_pipette : protocol_api.InstrumentContext = None):
		
		reagent_pipette = wash_pipette if reagent_pipette == None else reagent_pipette
		wash_pipette.configure_nozzle_layout(ALL)
		tip_manager.assign_tipracks(p200,TIPS200)
		if batch_1[0].parent.parent != mag_block:
			move_gripper(batch_1[0].parent['A1'],mag_block)
		remove_supernantant(wash_pipette,200,batch_1[0].parent['A1'],True)
		for i in range(2):
			#Add Ethanol With Airgap
			wash_pipette.pick_up_tip(ethanol_add['A1'])
			wash_pipette.aspirate(wash_volume,wash_buffer.bottom(ASPIRATE_HEIGHT),flow_rate=75)
			ctx.delay(seconds=1)
			wash_pipette.aspirate(10,wash_buffer.top(1),flow_rate=5) #Airgap
			wash_pipette.dispense(wash_volume+10,batch_1[0].parent['A1'].top(0),push_out=0,flow_rate=100)
			wash_pipette.aspirate(50,batch_1[0].parent['A1'].top(-1),flow_rate=50)
			wash_pipette.return_tip()
			#Incubate Ethanol
			if not DryRun:
				ctx.delay(seconds=30,msg='Incubating Ethanol')
			#Remove Ethanol
			wash_pipette.pick_up_tip(ethanol_removal['A1'])
			wash_pipette.aspirate(100,batch_1[0].parent['A1'].bottom(z=6),flow_rate=100)
			wash_pipette.aspirate(95,batch_1[0].parent['A1'].bottom(ASPIRATE_HEIGHT),flow_rate=25)
			wash_pipette.dispense(wash_pipette.current_volume,chute.top(-4),flow_rate=100)
			ctx.delay(seconds=1)
			wash_pipette.blow_out()
			wash_pipette.home_plunger()
			#wash_pipette.aspirate(10,flow_rate=100)
			if i == 1:#Aspirate twice on last
				wash_pipette.aspirate(50,batch_1[0].parent['A1'].bottom(ASPIRATE_HEIGHT-0.2),flow_rate=25)
				wash_pipette.dispense(wash_pipette.current_volume,chute.top(-4),flow_rate=100)
				ctx.delay(seconds=1)
				wash_pipette.blow_out()
			wash_pipette.return_tip()
		if not DryRun:
			ctx.delay(minutes=2,msg='Ethanol Evaporation')
		move_gripper(batch_1[0].parent,shaker_adapter)
		reagent_pipette.configure_nozzle_layout(COLUMN,start='A12')
		tip_manager.assign_tipracks(p200,TIPS50 if reagent_volume < 32 else TIPS200)
		hover_add_reagent(reagent_pipette,reagent_volume,reagent,batch if not DryRun or sample_count < 96 else [batch[0],batch[6],batch[-1]],tip_mix=10,mix_vol=0.8*reagent_volume, premix_reagent=premix_reagent)

	def remove_supernantant(pipette : protocol_api.InstrumentContext, volume : int | float, well : protocol_api.Well, save_tips : bool = False):
		pipette.configure_nozzle_layout(ALL)
		pipette.pick_up_tip(ethanol_removal['A1'])
		pipette.aspirate(volume*0.8,well.bottom(ASPIRATE_HEIGHT), flow_rate=50)
		pipette.aspirate(volume*0.2,well.bottom(ASPIRATE_HEIGHT-0.2), flow_rate=10)
		pipette.dispense(volume,chute.top(-4))
		ctx.delay(seconds=1)
		pipette.blow_out()
		pipette.home_plunger()
		if save_tips or DryRun:
			pipette.return_tip()
		else:
			pipette.drop_tip()
	
	def mix_in_reservoir(pipette : protocol_api.InstrumentContext, volume : int | float, well : protocol_api.Well, mix : int = 10):
		if well.length:
			dispense_loci = [well.bottom(ASPIRATE_HEIGHT).move(types.Point(z=1.5)),
						well.center(),
						well.center().move(types.Point(x=well.length/2-2,y=0)),
						well.center().move(types.Point(x=-well.length/2+2,y=0)),
						]
		if well.diameter:
			dispense_loci = [well.bottom(ASPIRATE_HEIGHT).move(types.Point(x=1.5,y=0)),
						well.center(),
						well.center().move(types.Point(y=-well.diameter/2*0.65)),
						well.center().move(types.Point(y=well.diameter/2*0.65)),
						well.bottom(ASPIRATE_HEIGHT),
						well.center().move(types.Point(x=-well.diameter/2*0.65)),
						well.center().move(types.Point(x=well.diameter/2*0.65)),
						well.bottom(ASPIRATE_HEIGHT),]
		dispense_loci = dispense_loci * math.ceil(mix/len(dispense_loci))
		dispense_loci = dispense_loci[:mix]
		dispense_loci.append(well.center().move(types.Point(x=well.length/2*0.6 if well.length else well.diameter/2*0.6)))
		pipette.aspirate(2,well.bottom(ASPIRATE_HEIGHT),flow_rate=volume/2)
		for loc in dispense_loci:
			pipette.aspirate(min(volume,198),well.bottom(ASPIRATE_HEIGHT),flow_rate=volume)
			ctx.delay(seconds=0.25)
			pipette.dispense(min(volume,198) if loc != dispense_loci[-1] else pipette.current_volume,loc,push_out=0, flow_rate=volume if loc != dispense_loci[-1] else volume/2)
		ctx.delay(seconds=4)
		if volume < 30:
			pipette.blow_out(well.top(-10))
		else:
			pipette.blow_out(well.top(-2))
		pipette.move_to(well.bottom(2),speed=10)
		pipette.move_to(well.top(2).move(types.Point(x=well.length/2-2 if well.length else well.diameter/2-2)),speed=10)

	def mix_in_well(pipette : protocol_api.InstrumentContext, volume : int | float, well : protocol_api.Well, mix : int = 10, shallow : bool = False):
		if well.length:
			dispense_loci = [well.bottom(ASPIRATE_HEIGHT).move(types.Point(z=1.5)),
						well.center(),
						well.center().move(types.Point(x=well.length/2-2,y=0)),
						well.center().move(types.Point(x=-well.length/2+2,y=0)),
						well.bottom(ASPIRATE_HEIGHT).move(types.Point(x=-1.5,y=0))]
		if well.diameter:
			dispense_loci = [well.bottom(ASPIRATE_HEIGHT).move(types.Point(z=1.5)),
						well.center(),
						well.center().move(types.Point(y=well.diameter/2*0.65)),
						well.bottom(ASPIRATE_HEIGHT),
						well.center().move(types.Point(y=-well.diameter/2*0.65)),
						well.center().move(types.Point(y=well.diameter/2*0.65)),
						well.bottom(ASPIRATE_HEIGHT),]
		if shallow:
			dispense_loci = [well.bottom(ASPIRATE_HEIGHT)]
		dispense_loci = dispense_loci * math.ceil(mix/len(dispense_loci))
		dispense_loci = dispense_loci[:mix]
		dispense_loci.append(well.center().move(types.Point(x=well.length/2*0.6 if well.length else well.diameter/2*0.6)))
		pipette.aspirate(2,well.bottom(ASPIRATE_HEIGHT),flow_rate=max(volume/2,2))
		for loc in dispense_loci:
			if not shallow:
				pipette.move_to(well.center())
			pipette.aspirate(volume,well.bottom(ASPIRATE_HEIGHT),flow_rate=max(volume,2))
			ctx.delay(seconds=0.25)
			if not shallow:
				pipette.move_to(well.center())
			pipette.dispense(volume if loc != dispense_loci[-1] else pipette.current_volume,loc,push_out=0, flow_rate=max(volume*2,2))
			ctx.delay(seconds=0.25)
		ctx.delay(seconds=4)
		pipette.blow_out(well.top(-2))
		pipette.move_to(well.bottom(2),speed=10)
		pipette.move_to(well.top(2).move(types.Point(x=well.length/2-2 if well.length else well.diameter/2-2)),speed=10)
	
	def add_reagent(pipette : protocol_api.InstrumentContext,
				  	volume : int | float,
					reagent : protocol_api.Well,
					well_list : list[protocol_api.Well],
					mix_after : bool = False,
					mix_time : None | float = None,
					tip_mix : int = 0,
					extra_pushout : bool = False,
					mix_vol : int | float = 10,
					premix_reagent : bool = True):
		if foiled_reagent_plate:
			if not pipette.has_tip:
				tip_manager.pick_up(pipette)
			pipette.touch_tip(reagent,radius=1.8,v_offset=-10,speed=15)
			pipette.drop_tip()
		for zed, well in enumerate(well_list):
			if type(reagent) == list:
				reagent = reagent[0 if zed <=6 else 1]
				if 0 if zed <=6 else 1: premix_reagent = True
			if not pipette.has_tip:
				tip_manager.pick_up(pipette)
			if extra_pushout:
				pipette.aspirate(10,reagent.top(ASPIRATE_HEIGHT))
			if  premix_reagent: #Dont mix if small number of columns, lose reagent
				pipette.mix(5,min(40,(volume-1)*(Columns-1)),reagent.bottom(ASPIRATE_HEIGHT),aspirate_flow_rate=max(volume/2,4),dispense_flow_rate=max(volume/2,4))
				premix_reagent = False
			if volume > 0:
				pipette.aspirate(volume,reagent.bottom(ASPIRATE_HEIGHT))
				pipette.move_to(reagent.bottom(ASPIRATE_HEIGHT+0.2),speed=1)
				ctx.delay(seconds=3)
			if extra_pushout:
				pipette.dispense(volume,well.bottom(ASPIRATE_HEIGHT), flow_rate=max(volume,4))
				ctx.delay(seconds=1)
				pipette.dispense(pipette.current_volume,well.top(-5),push_out=0 if tip_mix > 0 else None)
			else:
				pipette.dispense(volume,well.bottom(ASPIRATE_HEIGHT),push_out=0 if tip_mix > 0 else None, flow_rate=max(volume,4))
				ctx.delay(seconds=1)
					
			if tip_mix > 0:
				mix_in_well(pipette,mix_vol,well,tip_mix, shallow=True)
			else:
				ctx.delay(seconds=2)
				pipette.blow_out(well.top(-2))
				pipette.move_to(well.bottom(2),speed=10)
				pipette.move_to(well.top(-1).move(types.Point(x=well.length/2*0.75 if well.length else well.diameter/2*0.75)),speed=10)
			tip_manager.drop_tip(pipette, return_tip=False)
		if mix_after and well_list[0].parent.parent == shaker_adapter: #origionally was calling well_list[0].parent which returns labware not deck slot
			heater_shaker.set_and_wait_for_shake_speed(MIX_SPEED)
			if not DryRun:
				ctx.delay(seconds=30 if mix_time == None else mix_time)
			heater_shaker.deactivate_shaker()

	def hover_add_reagent(pipette : protocol_api.InstrumentContext,
				  		  volume : int | float,
						  reagent : protocol_api.Well,
						  well_list : list[protocol_api.Well],
						  mix_after : bool = False,
						  mix_time : None | float = None,
						  tip_mix : int = 0,
						  extra_pushout : bool = False,
						  mix_vol : int | float = 10,
						  premix_reagent : bool = True):
		if foiled_reagent_plate:
			if not pipette.has_tip:
				tip_manager.pick_up(pipette)
			pipette.touch_tip(reagent,radius=1.8,v_offset=-10,speed=15)
			pipette.drop_tip()
		tip_manager.pick_up(pipette)
		for zed, well in enumerate(well_list):
			if type(reagent) == list:
				reagent = reagent[0 if zed <=6 else 1]
				if 0 if zed <=6 else 1: premix_reagent = True
			#Transfer to all wells first with the same tip. 
			if extra_pushout:
				pipette.aspirate(10,reagent.top(ASPIRATE_HEIGHT))
			if  premix_reagent: #Dont mix if small number of columns, lose reagent
				pipette.mix(5,min(40,(volume-1)*(Columns-1)),reagent.bottom(ASPIRATE_HEIGHT),aspirate_flow_rate=max(volume/2,4),dispense_flow_rate=max(volume/2,4))
				premix_reagent = False
			if volume > 0:
				pipette.aspirate(volume,reagent.bottom(ASPIRATE_HEIGHT))
				pipette.move_to(reagent.bottom(ASPIRATE_HEIGHT+0.2),speed=1)
				ctx.delay(seconds=1.5)
				pipette.dispense(volume,well.top(-5),push_out=None, flow_rate=max(volume,4))
				ctx.delay(seconds=1)
				pipette.blow_out()
		#Go back into each wells to mix if needed. The first tip as above will be used for first well , and then mix with new tips per sample
		if tip_mix > 0:
			for zed, well in enumerate(well_list):
				if not pipette.has_tip:
					tip_manager.pick_up(pipette)
				mix_in_well(pipette,mix_vol,well,tip_mix, shallow=True)
				tip_manager.drop_tip(pipette, return_tip=False)
		if mix_after and well_list[0].parent.parent == shaker_adapter: #origionally was calling well_list[0].parent which returns labware not deck slot
			heater_shaker.set_and_wait_for_shake_speed(MIX_SPEED)
			if not DryRun:
				ctx.delay(seconds=30 if mix_time == None else mix_time)
			heater_shaker.deactivate_shaker()

	def add_rna_beads(pipette : protocol_api.InstrumentContext,
				  	volume : int | float,
					reagent : protocol_api.Well,
					well_list : list[protocol_api.Well],
					mix_after : bool = False,
					mix_time : None | float = None,
					tip_mix : int = 0,
					extra_pushout : bool = False,
					mix_vol : int | float = 10):
		for zed, well in enumerate(well_list):
			if not pipette.has_tip:
				tip_manager.pick_up(pipette)
			if extra_pushout:
				pipette.aspirate(10,reagent.top(ASPIRATE_HEIGHT))
			if volume > 0:
				pipette.aspirate(volume,reagent.bottom(ASPIRATE_HEIGHT))
				pipette.move_to(reagent.bottom(ASPIRATE_HEIGHT+0.2),speed=1)
				ctx.delay(seconds=3)
			if extra_pushout:
				pipette.dispense(volume,well.bottom(ASPIRATE_HEIGHT), flow_rate=max(volume,4))
				ctx.delay(seconds=1)
				pipette.dispense(pipette.current_volume,well.top(-5),push_out=0 if tip_mix > 0 else None)
			else:
				pipette.dispense(volume,well.bottom(ASPIRATE_HEIGHT),push_out=0 if tip_mix > 0 else None, flow_rate=max(volume,4))
				ctx.delay(seconds=1)
					
			if tip_mix > 0:
				mix_in_well(pipette,mix_vol,well,tip_mix, shallow=True)
			else:
				ctx.delay(seconds=2)
				pipette.blow_out(well.top(-2))
				pipette.move_to(well.bottom(2),speed=10)
				pipette.move_to(well.top(-1).move(types.Point(x=well.length/2*0.75 if well.length else well.diameter/2*0.75)),speed=10)
			tip_manager.drop_tip(pipette, return_tip=False)
		if mix_after and well_list[0].parent.parent == shaker_adapter: #origionally was calling well_list[0].parent which returns labware not deck slot
			heater_shaker.set_and_wait_for_shake_speed(MIX_SPEED)
			if not DryRun:
				ctx.delay(seconds=30 if mix_time == None else mix_time)
			heater_shaker.deactivate_shaker()

	def add_wash(pipette : protocol_api.InstrumentContext, volume : int | float, reagent : protocol_api.Well, well_list : list[protocol_api.Well], mix_after : bool = False, mix_time : None | float = None, tip_mix : int = 0):
		pushout_vol = 10
		for well in well_list:
			if type(reagent) == list:
				reagent = reagent[0 if well_list.index(well) <=6 else 1]
			if not pipette.has_tip:
				tip_manager.pick_up(pipette)
			if pushout_vol >= 0:
				pipette.aspirate(pushout_vol,reagent.top(1))
			pipette.aspirate(volume,reagent.bottom(ASPIRATE_HEIGHT))
			ctx.delay(seconds=1)
			pipette.aspirate(10,reagent.top(1),flow_rate=5) # Airgap
			pipette.dispense(pipette.current_volume,well.top(-4),push_out=0)
			if tip_mix > 0:
				mix_in_well(pipette,volume*0.9,well,tip_mix)
			else:
				ctx.delay(seconds=1)
				pipette.blow_out(well.top(-1))
				pipette.touch_tip(speed=40)
			tip_manager.drop_tip(pipette, return_tip=False)
		if mix_after and well_list[0].parent.parent == shaker_adapter:
			heater_shaker.set_and_wait_for_shake_speed(MIX_SPEED)
			if not DryRun:
				ctx.delay(seconds=30 if mix_time == None else mix_time)
			heater_shaker.deactivate_shaker()


	def move_gripper(labware : protocol_api.Labware,new_location): #shortened version of move_labware function
		if labware.parent == shaker_adapter:
			heater_shaker.open_labware_latch()
		ctx.move_labware(
		labware,
		new_location,
		use_gripper=True,
		)
		if new_location == shaker_adapter:
			heater_shaker.close_labware_latch()

	def move_offdeck(labware : protocol_api.Labware,new_location): #manually move labware from off deck locations onto deck
		if labware.parent == shaker_adapter:
			heater_shaker.open_labware_latch()
		ctx.move_labware(
		labware,
		new_location,
		use_gripper=False,
		)

	def wash_plate(wash_pipette : protocol_api.InstrumentContext,
					wash_volume : float, reagent : protocol_api.Well, reagent_volume : float, batch_1 : list[protocol_api.Well],
					mix_after : bool = False, mix_time : float = 30, premix_reagent : bool = False,
					reagent_pipette : protocol_api.InstrumentContext = None, batch_2 : list[protocol_api.Well] = None, mix_vol : float = 10):
		
		reagent_pipette = wash_pipette if reagent_pipette == None else reagent_pipette
		#First Batch
		remove_supernantant(wash_pipette,200,batch_1[0],True)
		move_gripper(batch_1[0].parent,shaker_adapter)
		heater_shaker.close_labware_latch()
		reagent_pipette.configure_nozzle_layout(COLUMN,start='A12')
		tip_manager.assign_tipracks(p200,TIPS50 if reagent_volume < 32 else TIPS200)
		#Commenting the below out since trasnfering functions do premix now
		#if premix_reagent:
		#	tip_manager.pick_up(reagent_pipette)
		#	reagent_pipette.mix(5,min(50,reagent_volume*Columns),reagent.bottom(ASPIRATE_HEIGHT))
		#	reagent_pipette.aspirate(reagent_volume,reagent.bottom(z=ASPIRATE_HEIGHT))
		#	reagent_pipette.dispense(reagent_volume,batch_1[0].bottom(ASPIRATE_HEIGHT),push_out=0)
		#	mix_in_well(reagent_pipette,mix_vol,batch_1[0],15)
		#	tip_manager.drop_tip(reagent_pipette, return_tip=False)
		#####batch_1[1:] if premix_reagent else #### use this in hover add if taking out premixing again
		hover_add_reagent(reagent_pipette,reagent_volume,reagent,batch_1,tip_mix=15,mix_vol=mix_vol, premix_reagent=premix_reagent)
	#Commands
	#									   CAPTURE MRNA
	################################################################################################
	#Break up into batches to avoid overdried beads, first batch- divide total Columns by 2 roundup
	active_plate = sample_1
	batch = active_plate.rows()[0][:Columns] #For single batch steps
	tip_manager.open_slot = mag_block
	thermo.open_lid()

	p200.configure_nozzle_layout(COLUMN,start='A12')
	tip_manager.assign_tipracks(p200,TIPS200)
	if start_point == 0:
		ctx.comment('Starting at Step 1: mRNA Capture')
		if DryRun==False:
			temp_block.set_temperature(4)
			thermo.open_lid()
			thermo.set_lid_temperature(100)
			thermo.set_block_temperature(25)
		heater_shaker.close_labware_latch()
		ctx.comment('---------Capture mRNA---------')

		tip_manager.pick_up(p200)
		p200.flow_rate.aspirate=1500
		p200.flow_rate.dispense=1500
		for x in range (20):
			p200.aspirate(min(RPBX_VOL*Columns,200),RPBX.bottom(ASPIRATE_HEIGHT),flow_rate=min(RPBX_VOL*Columns,200))
			p200.dispense(min(RPBX_VOL*Columns,200),RPBX.bottom(ASPIRATE_HEIGHT),push_out=0,flow_rate=min(RPBX_VOL*Columns,200))
		ctx.delay(seconds=3)
		p200.blow_out(RPBX.top(-12))
		p200.flow_rate.aspirate=80
		p200.flow_rate.dispense=100
		ctx.comment('Adding RPBX to samples')
		#Condidtional here for batch allows for quick testing in DryRun mode for sample counta over 96 (test 3 majors of plate). Else do full addition for smaller sample counts 
		add_rna_beads(p200,RPBX_VOL,RPBX,batch if not DryRun or sample_count < 96 else [batch[0],batch[6],batch[-1]],True,tip_mix=5,mix_vol=50/reaction_size*0.9)

		thermo.open_lid()
		move_gripper(active_plate,thermo)
		ctx.comment('Incubating for mRNA binding')
		thermo.close_lid()
		if DryRun==False:
			profile_mRNA_Cap = [
				{'temperature':65, 'hold_time_minutes': 5},
				{'temperature':4, 'hold_time_seconds':30},
				{'temperature':23, 'hold_time_minutes':5}
				]
			thermo.execute_profile(steps=profile_mRNA_Cap, repetitions=1, block_max_volume=50/reaction_size)
			thermo.set_block_temperature(23)
		thermo.open_lid()	
		#											 ELUTE MRNA
		####################################################################################################
		ctx.comment('---------Elute mRNA---------')
		move_gripper(active_plate,mag_block)
		if not DryRun:
			ctx.delay(minutes=2)
		ctx.comment('Removing RPX Supernatant')
		remove_supernantant(p200,200,active_plate['A1'], save_tips=True)
		move_gripper(active_plate,shaker_adapter)
		heater_shaker.close_labware_latch()
		ctx.comment('Adding Bead Wash Buffer')
		p200.configure_nozzle_layout(COLUMN,start='A12')
		tip_manager.assign_tipracks(p200,TIPS200)
		hover_add_reagent(p200,BWB_VOL,BWB,batch if not DryRun or sample_count < 96 else [batch[0],batch[6],batch[-1]],False,tip_mix=10, mix_vol=BWB_VOL*0.8,premix_reagent=False) #I think 60 is much too overkill, 10 was fine 11/17
		heater_shaker.set_and_wait_for_shake_speed(MIX_SPEED)
		if not DryRun:
			ctx.delay(minutes=2)
		heater_shaker.deactivate_shaker()
		move_gripper(active_plate,mag_block)
		if not DryRun:
			ctx.delay(minutes=2,msg='Collecting RNA beads on magnet')

		
		#remove super and replace with ELB
		ctx.comment('----Washing RNA Beads----')
		wash_plate(p200,BWB_VOL,ELB,ELB_VOL,batch if not DryRun or sample_count < 96 else [batch[0],batch[6],batch[-1]],True,30,False,p200,None,mix_vol=ELB_VOL*0.8) #Again the tip mixing is overkill here, hard coded  at 60
		heater_shaker.close_labware_latch()
		heater_shaker.set_and_wait_for_shake_speed(MIX_SPEED+250)
		if not DryRun:
			ctx.delay(minutes=INCUBATION_TIME,msg='Mixing Beads')
		heater_shaker.deactivate_shaker()
		heater_shaker.open_labware_latch()

		move_gripper(active_plate,thermo)
		thermo.close_lid()
		if DryRun==False:
			profile_mRNA_ELT = [
				{'temperature':80, 'hold_time_minutes': 2}
				]
			thermo.execute_profile(steps=profile_mRNA_ELT, repetitions=1, block_max_volume=25)
			thermo.set_block_temperature(25)
		thermo.open_lid()
		move_gripper(active_plate,shaker_adapter)
		heater_shaker.close_labware_latch()
		if stop_point == 3:
			move_gripper(Index_Plate,mag_block)
		if not DryRun:
			move_gripper(ethanol_removal,chute)
		else:
			move_offdeck(ethanol_removal,chute)
		next_tips = tip_manager.move_from_stacker(TIPS200)
		move_gripper(next_tips,adapters[0])
		if stop_point == 3:
			move_gripper(Index_Plate,stacker_200)
		ethanol_removal = next_tips

		#										   CLEANUP mRNA
		#####################################################################################################
		ctx.comment('---------Cleanup mRNA---------')
		ctx.comment('----Binding mRNA to RNA Beads----')
		tip_manager.assign_tipracks(p200,TIPS50)
		add_reagent(p200,BBB_VOL,BBB,batch if not DryRun or sample_count < 96 else [batch[0],batch[6],batch[-1]],True,120,tip_mix=5, mix_vol=(25+BBB_VOL)/reaction_size*0.9, premix_reagent=False) 
		heater_shaker.set_and_wait_for_shake_speed(MIX_SPEED)
		if not DryRun:
			ctx.delay(minutes=INCUBATION_TIME,msg='Incubating for RNA binding to beads')
		heater_shaker.deactivate_shaker()
		move_gripper(active_plate,mag_block)
		if not DryRun:
			ctx.delay(minutes=2,msg='Collecting RNA beads on magnet')
		ctx.comment('----Removing Supernatant----')
		remove_supernantant(p200,200,active_plate['A1'], save_tips=True)
		move_gripper(active_plate,shaker_adapter)
		heater_shaker.close_labware_latch()
		ctx.comment('----Washing RNA Beads with BWB----')
		p200.configure_nozzle_layout(COLUMN,start='A12')
		tip_manager.assign_tipracks(p200,TIPS200)
		hover_add_reagent(p200,BWB_VOL,BWB,batch if not DryRun or sample_count < 96 else [batch[0],batch[6],batch[-1]],False,tip_mix=10, mix_vol=BWB_VOL*0.8, premix_reagent=False) #I think 60 is much too overkill, 10 was fine 11/17
		heater_shaker.set_and_wait_for_shake_speed(MIX_SPEED)
		if not DryRun:
			ctx.delay(minutes=2,msg='Eluting Beads')
		heater_shaker.deactivate_shaker()
		move_gripper(active_plate,mag_block)
		if not DryRun:
			ctx.delay(minutes=3,msg='Collecting RNA beads on magnet')
		#Break up into batches to avoid overdried beads, first batch- divide total Columns by 2 roundup
		ctx.comment('----Washing RNA Beads and Eluting in Frag MM----')
		wash_plate(p200,BWB_VOL,FMM,FMM_VOL,batch if not DryRun or sample_count < 96 else [batch[0],batch[6],batch[-1]],False,0,True,p200,None,mix_vol=FMM_VOL*0.8) #Not on heater-shaker yet, move after and mix, MIX vol too high for FMM
		heater_shaker.close_labware_latch()
		heater_shaker.set_and_wait_for_shake_speed(MIX_SPEED)
		if not DryRun:
			ctx.delay(seconds=30,msg='Mixing Frag MM')
		heater_shaker.deactivate_shaker()
		ctx.delay(minutes=1.5,msg='Incubating Frag MM with RNA Beads')
		if not DryRun:
			move_gripper(ethanol_removal,chute)
		else:
			move_offdeck(ethanol_removal,chute)
		

		#										 FRAGMENT AND DENATURE MRNA
		######################################################################################################
		move_gripper(active_plate,thermo)
		ctx.comment('---------Fragment and Denature mRNA---------')
		thermo.close_lid()
		if DryRun==False:
			profile_DEN94_8 = [
				{'temperature':94, 'hold_time_minutes': 8}
				]
			thermo.execute_profile(steps=profile_DEN94_8, repetitions=1, block_max_volume=19/reaction_size)
			thermo.set_block_temperature(4)
		thermo.open_lid()

		move_gripper(active_plate,mag_block)
		heater_shaker.open_labware_latch()
		move_gripper(Index_Anchors,shaker_adapter)
		next_plate = stacker_pcr_plate.retrieve()
		move_gripper(next_plate,thermo)
		move_gripper(Index_Anchors,stacker_pcr_plate)
		new_50 = tip_manager.move_from_stacker(TIPS50)
		move_gripper(new_50,adapters[0])
		if not DryRun:
			ctx.delay(minutes=1,msg='Collecting RNA beads on magnet')
		ctx.comment('Transferring Fragmented mRNA to new column')
		p200.configure_nozzle_layout(ALL)
		tip_manager.assign_tipracks(p200,TIPS50)
		p200.pick_up_tip(new_50['A1'])
		p200.aspirate(FMM_VOL-2/reaction_size,active_plate['A1'], flow_rate=20)
		ctx.delay(seconds=0.5)
		p200.dispense(FMM_VOL-2/reaction_size,next_plate['A1'], flow_rate=40)
		tip_manager.drop_tip(p200, return_tip=DryRun)
		if not DryRun:
			move_gripper(active_plate,chute)
			move_gripper(new_50,chute)
		else:
			move_offdeck(active_plate,chute)
			move_offdeck(new_50,chute)
		active_plate = next_plate
		batch = active_plate.rows()[0][:Columns]
		next_tips = tip_manager.move_from_stacker(TIPS200)
		move_gripper(next_tips,adapters[0])
		if stop_point == 3:
			move_gripper(Index_Plate,stacker_200)
		ethanol_removal = next_tips
		#											FIRST STRAND SYNTHESIS
		##############################################################################################################
		ctx.comment('---------First Strand Synthesis---------')
		p200.configure_nozzle_layout(COLUMN,start='A12')
		tip_manager.assign_tipracks(p200,TIPS50)
		tip_manager.return_to_stacker = False
		move_gripper(active_plate,shaker_adapter)
		heater_shaker.close_labware_latch()
		ctx.comment('----Mixing FSMM before adding to samples----')
		ctx.comment('----Adding FSMM to samples----')
		add_reagent(p200,FSMM_VOL,FSMM,batch if not DryRun or sample_count < 96 else [batch[0],batch[6],batch[-1]],tip_mix=5,mix_vol=(FSMM_VOL+(FMM_VOL-2/reaction_size))*0.8)
		heater_shaker.set_and_wait_for_shake_speed(MIX_SPEED)
		if not DryRun:
			ctx.delay(seconds=30)
		heater_shaker.deactivate_shaker()
		move_gripper(active_plate,thermo)

		thermo.close_lid()
		if DryRun==False:
			profile_FSS = [
				{'temperature':25, 'hold_time_minutes': 10},
				{'temperature':42, 'hold_time_minutes':15},
				{'temperature':70, 'hold_time_minutes':15}
				]
			thermo.execute_profile(steps=profile_FSS, repetitions=1, block_max_volume=25)
			thermo.set_block_temperature(4)
		thermo.open_lid()


		#											   SECOND STRAND SYNTHESIS
		################################################################################################################
		ctx.comment('---------Second Strand Synthesis---------')
		if DryRun==False:
			thermo.set_lid_temperature(40)
		move_gripper(active_plate,shaker_adapter)
		heater_shaker.close_labware_latch()
		tip_manager.pick_up(p200)
		ctx.comment('----Mixing SMM before adding to samples----')
		add_reagent(p200,SMM_VOL,SMM,batch if not DryRun or sample_count < 96 else [batch[0],batch[6],batch[-1]],tip_mix=5,mix_vol=0.8*(50/reaction_size))
		heater_shaker.set_and_wait_for_shake_speed(MIX_SPEED)
		if not DryRun:
			ctx.delay(seconds=30)
		heater_shaker.deactivate_shaker()
		move_gripper(active_plate,thermo)

		thermo.close_lid()
		if DryRun==False:
			profile_SSS = [
				{'temperature':16, 'hold_time_minutes': 60}
			]
			thermo.execute_profile(steps=profile_SSS, repetitions=1, block_max_volume=50/reaction_size)
			thermo.set_block_temperature(4)
		thermo.open_lid()
		move_gripper(active_plate, shaker_adapter)
		heater_shaker.close_labware_latch()


	##################################################################################################################
	#CHUNK 2											   CLEANUP cDNA
	##################################################################################################################
	if start_point <= 1 and stop_point >= 1:
		if start_point == 1:
			thermo.open_lid()
		heater_shaker.close_labware_latch()
		ctx.comment('Starting Step 2: cDNA Cleanup')
		ctx.comment(f'---------CLEANUP cDNA ---------')
		if ctx.params.pause_for_beads:
			ctx.pause('Please add beads to the reservoir')
		p200.flow_rate.aspirate=2500
		p200.flow_rate.dispense=2500
		tip_manager.assign_tipracks(p200,TIPS200)
		tip_manager.pick_up(p200)
		#try blowing air to the bottom of the reservoir
		ctx.comment('----Preparing Ampure Beads----')
		if type(AMP) == list:
			for well in AMP:
				mix_in_reservoir(p200,min(200,AMP_VOL1*Columns),well,20)
		else:
			mix_in_reservoir(p200,min(200,AMP_VOL1*Columns),AMP,20)
		ctx.delay(seconds=4)
		p200.flow_rate.aspirate=80
		p200.flow_rate.dispense=100
		ctx.comment('----Adding Ampure Beads to samples----')
		add_wash(p200,AMP_VOL1,AMP,batch if not DryRun or sample_count < 96 else [batch[0],batch[6],batch[-1]],True,INCUBATION_TIME*60,tip_mix=3) 
		#Commenting out extra incubation to save time
		#if not DryRun:
		#	ctx.delay(minutes=INCUBATION_TIME,msg='Binding cDNA to beads')
		move_gripper(active_plate,mag_block)
		if not DryRun:
			ctx.delay(minutes=INCUBATION_TIME,msg='Collecting cDNA on magnet')
		ctx.comment('----Starting Ethanol Washes----')
		ethanol_wash(p200,ETOH,ETOH_VOL,RSB,RSB1_VOL,batch,False,p200) #editing here
		move_gripper(ethanol_removal,chute)


		#SHAKE TO ELUTE BEADS
		heater_shaker.set_and_wait_for_shake_speed(MIX_SPEED)
		if not DryRun:
			ctx.delay(minutes=INCUBATION_TIME,msg='Resuspending cleaned cDNA')
		heater_shaker.deactivate_shaker()
		move_gripper(active_plate,mag_block)
		if not DryRun:
			ctx.delay(minutes=2,msg='Collecting cleaned cDNA on magnet')

		#Move seconds sample plate to deck if needed


		#Transfer Samples
		##Move index plate, and grab new pcr plate, move a p50 tiprack to an adapter (ethanol removal which will be cleared at this point)
		move_gripper(Index_Anchors,shaker_adapter)
		next_plate = stacker_pcr_plate.retrieve()
		move_gripper(next_plate,thermo)
		move_gripper(Index_Anchors,stacker_pcr_plate)
		new_50 = tip_manager.move_from_stacker(TIPS50)
		move_gripper(new_50,adapters[0])
		if not DryRun:
			ctx.delay(minutes=1,msg='Collecting cDNA beads on magnet')
		ctx.comment('Transferring cleaned cDNA to new column')
		p200.configure_nozzle_layout(ALL)
		tip_manager.assign_tipracks(p200,TIPS50)
		p200.pick_up_tip(new_50['A1'])
		p200.aspirate(RSB1_VOL-2/reaction_size,active_plate['A1'].bottom(ASPIRATE_HEIGHT-0.2), flow_rate=20)
		ctx.delay(seconds=0.5)
		p200.dispense(RSB1_VOL-2/reaction_size,next_plate['A1'].bottom(ASPIRATE_HEIGHT), flow_rate=40)
		tip_manager.drop_tip(p200, return_tip=DryRun)
		if not DryRun:
			move_gripper(active_plate,chute)
			move_gripper(new_50,chute)
		else:
			move_offdeck(active_plate,chute)
			move_offdeck(new_50,chute)
		
		active_plate = next_plate
		batch = active_plate.rows()[0][:Columns]


	#												   dA-TAILING
	#################################################################################################################
	if start_point <= 2 and stop_point >= 2:
		p200.configure_nozzle_layout(COLUMN,start='A12')
		tip_manager.assign_tipracks(p200,TIPS50)
		thermo.open_lid()
		if DryRun==False:
			thermo.set_lid_temperature(100)
		if active_plate.parent != shaker_adapter:
			move_gripper(active_plate,shaker_adapter)
		if start_point == 2:
			heater_shaker.close_labware_latch()
		ctx.comment('---------Adenylate 3-Prime Ends---------')
		ctx.comment('Adding A-Tailing Mix to samples')
		add_reagent(p200,ATL4_VOL,ATL4,batch if not DryRun or sample_count < 96 else [batch[0],batch[6],batch[-1]],True,tip_mix=4,mix_vol=(ATL4_VOL+(RSB1_VOL-2))*0.8)
		move_gripper(active_plate,thermo)
		thermo.close_lid()
		if DryRun==False:
			profile_ATAIL = [
				{'temperature':37, 'hold_time_minutes': 30},
				{'temperature':70, 'hold_time_minutes':5}
				]
			thermo.execute_profile(steps=profile_ATAIL, repetitions=1, block_max_volume=30)
			thermo.set_block_temperature(4)

		thermo.open_lid()
		if ctx.params.manual_anchor > 1:
			move_gripper(Index_Anchors,shaker_adapter)
			heater_shaker.close_labware_latch()
		#											   LIGATE ANCHORS
		#################################################################################################################
		if ctx.params.manual_anchor == 3:
			ctx.comment('-------- DILUTING ANCHORS --------')
			ANCHOR_VOL = 2.5 / reaction_size
			p200.configure_nozzle_layout(COLUMN,start='A12')
			tip_manager.assign_tipracks(p200,TIPS50)
			add_wash(p200,LIGX_VOL,RSB,batch if not DryRun or sample_count < 96 else [batch[0],batch[6],batch[-1]],True)
		ctx.comment('---------LIGATE ANCHORS---------')
		if 1 !=  ctx.params.manual_anchor:
			if sample_count == 96:
				p200.configure_nozzle_layout(ALL)
				p200.configure_for_volume(LIGX_VOL)
				if start_point < 2:
					new_50 = tip_manager.move_from_stacker(TIPS50)
					move_gripper(new_50,adapters[0])
				p200.flow_rate.aspirate=2.5
				p200.pick_up_tip(new_50['A1']) 
				p200.move_to(Anchors[0].top(-10))
				p200.touch_tip(Anchors[0],radius=1.8,v_offset=-10,speed=15) 
				#Reseat tips
				p200.return_tip()
				p200.pick_up_tip(new_50['A1'])
				for i in range(2):
					p200.aspirate(ANCHOR_VOL,Anchors[0].bottom(z=ASPIRATE_HEIGHT))
					p200.dispense(ANCHOR_VOL,Anchors[0].bottom(z=ASPIRATE_HEIGHT),push_out=0)
				p200.aspirate(ANCHOR_VOL,Anchors[0].bottom(z=ASPIRATE_HEIGHT))
				ctx.delay(seconds=3)
				p200.dispense(ANCHOR_VOL,active_plate['A1'].top(-10))
				for x in range(1):
					p200.aspirate(25/reaction_size,active_plate['A1'].bottom(z=1),flow_rate=12.5/reaction_size)
					p200.dispense(25/reaction_size,active_plate['A1'].bottom(z=1),push_out=0)
				p200.blow_out(active_plate['A1'].top(-5))
				p200.move_to(active_plate['A1'].bottom(2),speed=10)
				p200.move_to(active_plate['A1'].top(-1).move(types.Point(x=active_plate['A1'].length/2*0.75 if active_plate['A1'].length else active_plate['A1'].diameter/2*0.75)),speed=10)
				tip_manager.drop_tip(p200, return_tip=DryRun)
			else:
				move_gripper(active_plate,mag_block)
				p200.configure_nozzle_layout(COLUMN,start='A12')
				tip_manager.assign_tipracks(p200,TIPS50)
				ctx.comment('----Adding Index Anchors to samples----')
				ANCHOR_VOL = 2.5 / reaction_size
				p200.flow_rate.aspirate=2.5
				for x,well in enumerate(batch):
					tip_manager.pick_up(p200)
					p200.move_to(Anchors[x].top(-10))
					p200.touch_tip(Anchors[x],radius=1.8,v_offset=-10,speed=15)
					for i in range(2):
						p200.aspirate(ANCHOR_VOL,Anchors[x].bottom(z=ASPIRATE_HEIGHT))
						p200.dispense(ANCHOR_VOL,Anchors[x].bottom(z=ASPIRATE_HEIGHT),push_out=0)
					p200.aspirate(ANCHOR_VOL,Anchors[x].bottom(z=ASPIRATE_HEIGHT))
					ctx.delay(seconds=3)
					p200.dispense(ANCHOR_VOL,well.top(-10))
					for x in range(1): #reduce this 'mix' to 1 because we mix with ligation mix, just flush tip
						p200.aspirate(25/reaction_size,well.bottom(z=1),flow_rate=12.5/reaction_size)
						p200.dispense(25/reaction_size,well.bottom(z=1),push_out=0)
					p200.blow_out(well.top(-5))
					p200.move_to(well.bottom(2),speed=10)
					p200.move_to(well.top(-1).move(types.Point(x=well.length/2*0.75 if well.length else well.diameter/2*0.75)),speed=10)	
					tip_manager.drop_tip(p200, return_tip=False)
		else:
			ctx.pause('Please add Index Anchors to the Index Anchor Plate on the deck and click Resume')
		if 1 != ctx.params.manual_anchor:
			move_gripper(Index_Anchors,stacker_pcr_plate)
			if sample_count == 96: 
				if DryRun:
					move_offdeck(new_50,chute)
				else:
					move_gripper(new_50,chute)
		ctx.comment('----Adding Ligation Mix to samples----')
		if active_plate.parent != shaker_adapter:
			move_gripper(active_plate,shaker_adapter)
		p200.configure_nozzle_layout(COLUMN,start='A12')
		tip_manager.assign_tipracks(p200,TIPS50)
		add_reagent(p200,LIGX_VOL,LIGX,batch if not DryRun or sample_count < 96 else [batch[0],batch[6],batch[-1]],False,mix_vol=(30)*0.8,tip_mix=4)
		move_gripper(active_plate,thermo)
		thermo.close_lid()
		ctx.comment('Incubating for Ligation')
		if DryRun==False:
			profile_LIG = [
				{'temperature':30, 'hold_time_minutes': 12}
				]
			thermo.execute_profile(steps=profile_LIG, repetitions=1, block_max_volume=38/reaction_size)
			thermo.set_block_temperature(4)
		thermo.open_lid()

		#											   STOP LIGATION
		############################################################################################################
		ctx.comment('---------STOP LIGATION---------')
		p200.configure_for_volume(5)
		move_gripper(active_plate,shaker_adapter)
		add_reagent(p200,STL_VOL,STL,batch if not DryRun or sample_count < 96 else [batch[0],batch[6],batch[-1]],tip_mix=5, extra_pushout=True,mix_vol=(38/reaction_size)*0.9)
		heater_shaker.set_and_wait_for_shake_speed(MIX_SPEED)
		if not DryRun:
			ctx.delay(seconds=30,msg='Mixing to stop ligation')
		heater_shaker.deactivate_shaker()


		#											  CLEANUP FRAGMENTS
		#############################################################################################################
		if start_point == 0 and stop_point == 3 and sample_count > 48:
			stacker_200.fill(stacker_200.get_current_storable_labware(),'Fill the B4 stacker fully with 200uL Tips')
			stacker_50_1.fill(stacker_50_1.get_current_storable_labware(),'Fill the D4 stacker fully with 50uL Tips')
			tip_manager.stackers[TIPS200][0][1] = 6
			tip_manager.stackers[TIPS50][0][1] = 6
			
		ctx.comment(f'---------CLEANUP FRAGMENTS ---------')
		p200.flow_rate.aspirate=2500
		p200.flow_rate.dispense=2500
		if ctx.params.pause_for_beads:
			ctx.pause('Please add beads to the reservoir')
		tip_manager.assign_tipracks(p200,TIPS200)
		tip_manager.pick_up(p200)
		if type(AMP) == list:
			for well in AMP:
				mix_in_reservoir(p200,min(200,AMP_VOL2*Columns),well,20)
		else:
			mix_in_reservoir(p200,AMP_VOL2*Columns,AMP,20)
		ctx.delay(seconds=4)
		p200.flow_rate.aspirate=80
		p200.flow_rate.dispense=100
		ctx.comment('----Adding Second Magbeads to fragments----')
		add_wash(p200,AMP_VOL2,AMP,batch if not DryRun or sample_count < 96 else [batch[0],batch[6],batch[-1]],True,INCUBATION_TIME*60,tip_mix=3) #Use wash to add beads since it adds to top
		#Commenting out extra incubation to save time
		#if not DryRun:
		#	ctx.delay(minutes=INCUBATION_TIME,msg='Binding fragments to beads')
		move_gripper(active_plate,mag_block)
		if not DryRun:
			ctx.delay(minutes=INCUBATION_TIME,msg='Collecting fragments on magnet')
		ctx.comment('----Washing beads with Ethanol----')
		if stop_point == 3:
			move_gripper(Index_Plate,shaker_adapter)
		next_tips = tip_manager.move_from_stacker(TIPS200)
		move_gripper(next_tips,adapters[0])
		if stop_point == 3:
			move_gripper(Index_Plate,stacker_200)
		ethanol_removal = next_tips
		ethanol_wash(p200,ETOH,ETOH_VOL,RSB,RSB2_VOL,batch,False,p200)
		move_gripper(ethanol_removal,chute)

		#Shake and incubate to elute beads
		ctx.comment('----Eluting cleaned fragments off beads----')
		heater_shaker.set_and_wait_for_shake_speed(MIX_SPEED)
		if not DryRun:
			ctx.delay(minutes=1,msg='Resuspending Fragments')
		heater_shaker.deactivate_shaker()
		if not DryRun:
			ctx.delay(minutes=INCUBATION_TIME+3,msg='Eluting Fragments off beads')
		move_gripper(active_plate,mag_block)
		if not DryRun:
			ctx.delay(minutes=2,msg='Collecting eluted fragments on magnet')

		#Transfer Samples
		ctx.comment('----Transferring Final Libraries to New Columns----')
		move_gripper(Index_Anchors,shaker_adapter)
		next_plate = stacker_pcr_plate.retrieve()
		move_gripper(next_plate,thermo)
		move_gripper(Index_Anchors,stacker_pcr_plate)
		new_50 = tip_manager.move_from_stacker(TIPS50)
		move_gripper(new_50,adapters[0])
		if not DryRun:
			ctx.delay(minutes=1,msg='Collecting cDNA beads on magnet')
		ctx.comment('Transferring cleaned cDNA to new column')
		p200.configure_nozzle_layout(ALL)
		tip_manager.assign_tipracks(p200,TIPS50)
		p200.pick_up_tip(new_50['A1'])
		p200.aspirate(RSB2_VOL-2/reaction_size,active_plate['A1'].bottom(ASPIRATE_HEIGHT-0.2), flow_rate=20)
		ctx.delay(seconds=0.5)
		p200.dispense(RSB2_VOL-2/reaction_size,next_plate['A1'].bottom(ASPIRATE_HEIGHT), flow_rate=40)
		tip_manager.drop_tip(p200, return_tip=DryRun)
		if not DryRun:
			move_gripper(active_plate,chute)
			move_gripper(new_50,chute)
		else:
			move_offdeck(active_plate,chute)
			move_offdeck(new_50,chute)
		
		active_plate = next_plate
		batch = active_plate.rows()[0][:Columns]

	#SAFE STOPPING POINT
	#CHUNK 4										   AMPLIFY LIBRARIES
	###################################################################################################
	if start_point <= 3 and stop_point >= 3:
		ctx.comment('Starting Step 3: Library Amplification')
		ctx.comment('---------AMPLIFY LIBRARIES---------')
		
		#move P50 to adapter for index addition
		if active_plate.parent != thermo:
			move_gripper(active_plate,thermo)
		#Will need to add an else statement here
		if start_point < 3:
			new_50 = tip_manager.move_from_stacker(TIPS50)
			move_gripper(new_50,adapters[0])
		thermo.open_lid()
		#Have plate start on thermo, and index be on deck if start point == 3
		move_gripper(Index_Plate,shaker_adapter)
		heater_shaker.close_labware_latch()
		ctx.comment('----Adding Indexes to samples----')
        #poke holes in Index Plate Seal
		if sample_count == 96:
			p200.configure_nozzle_layout(ALL)
			p200.pick_up_tip(new_50['A1']) #change to manual pick up with p50
			p200.move_to(Index_Adap[0].top(-9))
			p200.touch_tip(Index_Adap[0],radius=1.8,v_offset=-10,speed=15)
			#Return tip and pick up again to reseat
			p200.return_tip()
			p200.pick_up_tip(new_50['A1'])
			for i in range(2):
				p200.aspirate(INDEX_VOL,Index_Adap[0].bottom(z=ASPIRATE_HEIGHT))
				p200.dispense(INDEX_VOL,Index_Adap[0].bottom(z=ASPIRATE_HEIGHT),push_out=0)
			p200.aspirate(INDEX_VOL,Index_Adap[0].bottom(z=ASPIRATE_HEIGHT))
			ctx.delay(seconds=1)
			p200.dispense(INDEX_VOL,batch[0].top(-10))
			p200.blow_out(batch[0].top(-5))
			p200.move_to(batch[0].bottom(2),speed=10)
			p200.move_to(batch[0].top(-1).move(types.Point(x=batch[0].length/2*0.75 if batch[0].length else batch[0].diameter/2*0.75)),speed=10)
			tip_manager.drop_tip(p200, return_tip=False)
		else:
			p200.configure_nozzle_layout(COLUMN,start='A12')
			tip_manager.assign_tipracks(p200,TIPS50)
			move_gripper(active_plate,mag_block)
			for x,well in enumerate(batch):
				p200.flow_rate.aspirate=2
				tip_manager.pick_up(p200)
				p200.move_to(Index_Adap[x].bottom(z=ASPIRATE_HEIGHT))
				p200.touch_tip(Index_Adap[x],radius=1.8,v_offset=-10,speed=15) 
				for i in range(2):
					p200.aspirate(INDEX_VOL,Index_Adap[x].bottom(z=ASPIRATE_HEIGHT))
					p200.dispense(INDEX_VOL,Index_Adap[x].bottom(z=ASPIRATE_HEIGHT),push_out=0)
				ctx.delay(seconds=3)
				p200.aspirate(INDEX_VOL,Index_Adap[x].bottom(z=ASPIRATE_HEIGHT))
				ctx.delay(seconds=3)
				p200.flow_rate.dispense=8
				p200.dispense(INDEX_VOL-1,well.top(-10))
				p200.blow_out(well.top(-5))
				p200.move_to(well.bottom(2),speed=10)
				p200.move_to(well.top(-1).move(types.Point(x=well.length/2*0.75 if well.length else well.diameter/2*0.75)),speed=10)	
				tip_manager.drop_tip(p200, return_tip=False)
		move_gripper(Index_Plate,stacker_200) #check if we can toss this
		move_gripper(active_plate,shaker_adapter)
		ctx.comment('----Adding PCR Master Mix-----')	
		p200.configure_nozzle_layout(COLUMN,start='A12')
		tip_manager.assign_tipracks(p200,TIPS50)
		add_reagent(p200,EPM_VOL,EPM,batch if not DryRun or sample_count < 96 else [batch[0],batch[6],batch[-1]],tip_mix=4,extra_pushout=True,mix_vol=(RSB2_VOL+EPM_VOL+INDEX_VOL)*0.8)
		move_gripper(active_plate,thermo)
		if not DryRun:
			move_gripper(new_50,chute)
		else:
			move_offdeck(new_50,chute)
		next_tips = tip_manager.move_from_stacker(TIPS200)
		move_gripper(next_tips,adapters[0])
		ethanol_removal = next_tips
		

		thermo.close_lid()
		ctx.comment('----Starting PCR Amplification----')
		if DryRun==False:
			profile_PCR_1 = [
				{'temperature':98, 'hold_time_seconds': 30}
				]
			thermo.execute_profile(steps=profile_PCR_1, repetitions=1, block_max_volume=50)
			profile_PCR_2 =[
				{'temperature':98,'hold_time_seconds':10},
				{'temperature':60,'hold_time_seconds':30},
				{'temperature':72,'hold_time_seconds':30}
				]
			thermo.execute_profile(steps=profile_PCR_2, repetitions=PCR_Cycles,block_max_volume=50)
			profile_PCR_3 = [
				{'temperature':72, 'hold_time_minutes': 5}
				]
			thermo.execute_profile(steps=profile_PCR_3, repetitions=1, block_max_volume=50)
			thermo.set_block_temperature(4)
		thermo.open_lid()


		#										   CLEANUP LIBRARIES
		########################################################################################################
		ctx.comment(f'---------CLEANUP LIBRARIES ---------')
		p200.flow_rate.aspirate=2000
		p200.flow_rate.dispense=2000
		move_gripper(active_plate,shaker_adapter)
		heater_shaker.close_labware_latch()
		if ctx.params.pause_for_beads:
			ctx.pause('Please add beads to the reservoir')
		thermo.deactivate_lid()
		thermo.deactivate_block()
		p200.configure_nozzle_layout(COLUMN,start='A12')
		tip_manager.assign_tipracks(p200,TIPS200)
		tip_manager.pick_up(p200)
		if type(AMP) == list:
			for well in AMP:
				mix_in_reservoir(p200,min(AMP_VOL3*Columns,200),well,20)
		else:
			mix_in_reservoir(p200,AMP_VOL3*Columns,AMP,20)
		ctx.delay(seconds=4)
		p200.flow_rate.aspirate=80
		p200.flow_rate.dispense=100

		ctx.comment('----Adding Ampure Beads to samples----')
		add_wash(p200,AMP_VOL3,AMP,batch if not DryRun or sample_count < 96 else [batch[0],batch[6],batch[-1]],True,INCUBATION_TIME*60,tip_mix=3)
		#Commenting out extra incubation to save time
		#if not DryRun:
		#	ctx.delay(minutes=3,msg='Incubating to bind libraries to beads')
		move_gripper(active_plate,mag_block)
		if not DryRun:
			ctx.delay(minutes=INCUBATION_TIME,msg='Collecting beads on magnet')
		ctx.comment('----Performing Ethanol Washes----')
		ethanol_wash(p200,ETOH,ETOH_VOL,RSB,RSB3_VOL,batch,False,p200)
		move_gripper(ethanol_removal,chute)
		#Shake and incubate to elute beads
		heater_shaker.set_and_wait_for_shake_speed(MIX_SPEED)
		if not DryRun:
			ctx.delay(minutes=INCUBATION_TIME+4,msg='Incubating to elute libraries off beads')
		heater_shaker.deactivate_shaker()	
		move_gripper(active_plate,mag_block)
		if not DryRun:
			ctx.delay(minutes=2,msg='Collecting beads on magnet')

		batch : list[protocol_api.Well] = []
		#Transfer Samples
		ctx.comment('----Transferring Final Libraries to New Columns----')
		move_gripper(Index_Anchors,shaker_adapter)
		next_plate = stacker_pcr_plate.retrieve()
		move_gripper(next_plate,thermo)
		move_gripper(Index_Anchors,stacker_pcr_plate)
		new_50 = tip_manager.move_from_stacker(TIPS50)
		move_gripper(new_50,adapters[0])
		if not DryRun:
			ctx.delay(minutes=1,msg='Collecting cDNA beads on magnet')
		ctx.comment('Transferring cleaned cDNA to new column')
		p200.configure_nozzle_layout(ALL)
		tip_manager.assign_tipracks(p200,TIPS50)
		p200.pick_up_tip(new_50['A1'])
		p200.aspirate(RSB3_VOL-2/reaction_size,active_plate['A1'].bottom(ASPIRATE_HEIGHT-0.2), flow_rate=20)
		ctx.delay(seconds=0.5)
		p200.dispense(RSB3_VOL-2/reaction_size,next_plate['A1'].bottom(ASPIRATE_HEIGHT), flow_rate=40)
		tip_manager.drop_tip(p200, return_tip=DryRun)
		if not DryRun:
			move_gripper(active_plate,chute)
			move_gripper(new_50,chute)
		else:
			move_offdeck(active_plate,chute)
			move_offdeck(new_50,chute)
		active_plate = next_plate
		batch = active_plate.rows()[0][:Columns]

	ctx.pause('Protocol complete. Press continue to deactivate temperature blocks.')
	if ctx.params.bake_off:
		ctx.comment('Starting Bake Off Procedure')
		temp_block.start_set_temperature(65)
		thermo.set_block_temperature(65)
		ctx.delay(minutes=15)
		ctx.comment('Bake Off Complete. Deactivating Temperature Blocks.')
	temp_block.deactivate()
	thermo.deactivate_block()
	thermo.deactivate_lid()
	thermo.open_lid()
	heater_shaker.open_labware_latch()
	ctx.home()
	overage= 1.2
#												  Liquid Definitions and Assignments
 #######################################################################################################################################################
	#Cold Reagent Liquids
	#Load Step 1 Reagents 
	bead_volume_needed = 11
	rsb_volume_needed = 11
	deepwell_Dead_volume = 20
	if start_point == 0:
		ELB_=ctx.define_liquid(name="Elution Buffer", description="Elution Buffer",display_color="#ff6699")
		FMM_=ctx.define_liquid(name="Fragmentation Master Mix",description="Fragmentation Master Mix", display_color="#cc3399")
		FSMM_=ctx.define_liquid(name="First Strand Master Mix", description="First Strand Master Mix",display_color="#0066ff")
		SMM_=ctx.define_liquid(name="SMM", description="Second Strand Marking Master Mix",display_color="#00cc99")
		RPBX_=ctx.define_liquid(name="RNA Purification Beads", description="RNA Purification Beads",display_color="#66ffff")
		BWB_=ctx.define_liquid(name="Bead Washing Buffer", description="Bead Washing Buffer", display_color="#800080")
		BBB_=ctx.define_liquid(name="Bead Binding Buffer",description="Bead Binding Buffer",display_color="#1ad1ff")

		if reaction_size == 1 and Columns > 6:
			for well in reagent_plate.wells()[0:8]:
				well.load_liquid(liquid=FMM_, volume=FMM_VOL*6*overage)
			for well in reagent_plate.wells()[8:16]:
				well.load_liquid(liquid=FMM_, volume=FMM_VOL*(Columns-6)*overage)
			for well in reagent_plate.wells()[16:24]:
				well.load_liquid(liquid=ELB_, volume=ELB_VOL*6*overage)
			for well in reagent_plate.wells()[24:32]:
				well.load_liquid(liquid=ELB_, volume=ELB_VOL*(Columns-6)*overage)
			for well in reagent_plate.wells()[32:40]:
				well.load_liquid(liquid=FSMM_, volume=FSMM_VOL*Columns*overage)
			for well in reagent_plate.wells()[40:48]:
				well.load_liquid(liquid=SMM_, volume=SMM_VOL*6*overage)
			for well in reagent_plate.wells()[48:56]:
				well.load_liquid(liquid=SMM_, volume=SMM_VOL*(Columns-6)*overage)
			Reservior.load_liquid(wells=Reservior.columns()[0],liquid=RPBX_, volume=RPBX_VOL*Columns*overage+deepwell_Dead_volume)
			Reservior.load_liquid(wells=Reservior.columns()[1],liquid=BWB_, volume=BWB_VOL*6*overage*2+deepwell_Dead_volume)
			Reservior.load_liquid(wells=Reservior.columns()[2],liquid=BWB_, volume=BWB_VOL*(Columns-6)*overage*2+deepwell_Dead_volume)
			Reservior.load_liquid(wells=Reservior.columns()[3],liquid=BBB_, volume=BBB_VOL*Columns*overage+deepwell_Dead_volume)

		else:
			for well in reagent_plate.wells()[0:8]:
				well.load_liquid(liquid=FMM_, volume=FMM_VOL*Columns*overage)
			for well in reagent_plate.wells()[8:16]:
				well.load_liquid(liquid=ELB_, volume=ELB_VOL*Columns*overage)
			for well in reagent_plate.wells()[16:24]:
				well.load_liquid(liquid=FSMM_, volume=FSMM_VOL*Columns*overage)
			for well in reagent_plate.wells()[24:32]:
				well.load_liquid(liquid=SMM_, volume=SMM_VOL*Columns*overage)
			Reservior.load_liquid(wells=Reservior.columns()[0],liquid=RPBX_, volume=RPBX_VOL*Columns*overage+deepwell_Dead_volume)
			Reservior.load_liquid(wells=Reservior.columns()[1],liquid=BWB_, volume=BWB_VOL*2*Columns*overage+deepwell_Dead_volume)
			Reservior.load_liquid(wells=Reservior.columns()[2],liquid=BBB_, volume=BBB_VOL*Columns*overage+deepwell_Dead_volume)
	#Load Step 2 Reagents
	if start_point <= 1 and stop_point > 0:
		bead_volume_needed = bead_volume_needed + AMP_VOL1
		rsb_volume_needed = rsb_volume_needed + RSB1_VOL
	if start_point <= 2 and stop_point > 1:
		ATL4_=ctx.define_liquid(name="ATL4", description="A-Tailing Mix",display_color="#6699ff")
		LIGX_=ctx.define_liquid(name="LIGX",description="Ligation Mix",display_color="#ff9933")
		STL_=ctx.define_liquid(name="STL", description="Stop Ligation Buffer",display_color="#ffff99")
		if reaction_size == 1 and Columns > 6:
			for well in reagent_plate.wells()[56:64]:
				well.load_liquid(liquid=ATL4_, volume=ATL4_VOL*Columns*overage)
			for well in reagent_plate.wells()[64:72]:
				well.load_liquid(liquid=LIGX_, volume=LIGX_VOL*Columns*overage)
			for well in reagent_plate.wells()[72:80]:
				well.load_liquid(liquid=STL_, volume=STL_VOL*Columns*overage)
		else:
			for well in reagent_plate.wells()[32:40]:
				well.load_liquid(liquid=ATL4_, volume=ATL4_VOL*Columns*overage)
			for well in reagent_plate.wells()[40:48]:
				well.load_liquid(liquid=LIGX_, volume=LIGX_VOL*Columns*overage)
			for well in reagent_plate.wells()[48:56]:
				well.load_liquid(liquid=STL_, volume=STL_VOL*Columns*overage)
		bead_volume_needed = bead_volume_needed + AMP_VOL2
		rsb_volume_needed = rsb_volume_needed + RSB2_VOL
	if start_point <= 3 and stop_point > 2:
		EPM_=ctx.define_liquid(name="EPM",description="Enhanced PCR Mix",display_color="#cc99ff")
		if reaction_size == 1 and Columns > 6:
			for well in reagent_plate.wells()[80:88]:
				well.load_liquid(liquid=EPM_, volume=EPM_VOL*6*overage)
			for well in reagent_plate.wells()[88:96]:
				well.load_liquid(liquid=EPM_, volume=EPM_VOL*(Columns-6)*overage)
		else:
			for well in reagent_plate.wells()[56:64]:
				well.load_liquid(liquid=EPM_, volume=EPM_VOL*Columns*overage)
		bead_volume_needed = bead_volume_needed + AMP_VOL3
		rsb_volume_needed = rsb_volume_needed + RSB3_VOL
	#Reservoir Liquids
	if bead_volume_needed > 10:
		AMP_=ctx.define_liquid(name="Ampure XP Beads",description="Ampure XP Beads",display_color="#663300")
		RSB_=ctx.define_liquid(name="Resuspension Buffer", description="Resuspension Buffer",display_color="#b3ffb3")
		if reaction_size == 1 and Columns > 6:
			Reservior.load_liquid(wells=Reservior.columns()[4],liquid=AMP_,volume=bead_volume_needed*6*overage+deepwell_Dead_volume)
			Reservior.load_liquid(wells=Reservior.columns()[5],liquid=AMP_,volume=bead_volume_needed*(Columns-6)*overage+deepwell_Dead_volume)
			Reservior.load_liquid(wells=Reservior.columns()[6],liquid=RSB_,volume=rsb_volume_needed*Columns*overage+deepwell_Dead_volume)
		else:
			Reservior.load_liquid(wells=Reservior.columns()[3],liquid=AMP_,volume=bead_volume_needed*Columns*overage+deepwell_Dead_volume)
			Reservior.load_liquid(wells=Reservior.columns()[4],liquid=RSB_,volume=rsb_volume_needed*Columns*overage+deepwell_Dead_volume)		
		ETOH_=ctx.define_liquid(name="80% Ethanol", description="80% Ethanol",display_color="#f2f2f2")
		ethanol_reservoir.load_liquid(wells=ethanol_reservoir.columns()[0],liquid=ETOH_, volume=250000)
	#Samples
	Samples=ctx.define_liquid(name="Input Sample",description="Input RNA/cDNA/Library for relevant step",display_color="#99ff99")
	for well in sample_1.wells()[:Columns*8]:
		well.load_liquid(liquid=Samples, volume=25)
	ctx.comment(f'{tip_manager.tip_rack_counts}')
	ctx.comment(f'{tip_manager.stackers}')